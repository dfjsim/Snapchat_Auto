"""The GUI side of retrieving Memories media from Snapchat's servers: the Cloud window and the
progress window.

The request is built from the window through **the same function the command line uses**
(``Snapchat_Auto._cloud_request``, handed in as ``ui.build_request``), so the two front ends cannot
ask for different things from the same answers. The legal-authority attestation and note are never
prefilled and never saved; Start stays disabled until both are given.

The date filter is a table of rules — a timestamp, a range with either end open, include or exclude.
To avoid typing one range again and again there is a quick-entry line (a range, then *Add for checked*
over any set of timestamp types) and *Copy range to…* on every row. The table's logic is plain
functions (:func:`add_for_fields`, :func:`rules_spec`), testable without a display.

The engine runs on a worker thread; the progress window, on the main one, shows each event and lets
the examiner pause, resume, stop and change the pace while it runs (:class:`cloud_download.Control`).
"""
import json
import os
import queue
import threading
import time

from scripts import cloud_download, cloud_memories

#: How many rule rows the window offers. FreeSimpleGUI layouts are fixed once built, so rows are
#: shown and hidden rather than added.
MAX_RULES = 10
MODES = ("include", "exclude")


# ------------------------------------------------------------------------------- the rule table

def field_choices():
    """The timestamps a rule can name, in the order the window lists them, with ``*`` last."""
    return cloud_memories.all_fields() + ["*"]


def short_name(field):
    """What the window calls a timestamp: the report's own label, which is unique across both tables
    ("Created" is the snap's, "Entry created" the entry's); the column is in the tooltip and the help."""
    from scripts.memories_media_report import ENTRY_TIME_LABELS, SNAP_TIME_LABELS
    table, _, column = field.partition(".")
    if field == "*":
        return "every timestamp (*)"
    if field == cloud_memories.MEMDATA_FIELD:
        return "MemData id created"
    labels = SNAP_TIME_LABELS if table == "ZGALLERYSNAP" else ENTRY_TIME_LABELS
    return labels.get(column, field)


def add_for_fields(rows, fields, start, end, mode):
    """Rows after giving ``fields`` the range ``start``–``end`` and ``mode``.

    A field that already has a row **with the same mode** gets that row's range updated; any other
    field gets a new row. Used by *Add for checked* and by *Copy range to…*, which is why both produce
    the same rules for the same answers.
    """
    rows = [dict(r) for r in rows]
    for name in fields:
        for row in rows:
            if row["field"] == name and row["mode"] == mode:
                row["from"], row["to"] = start, end
                break
        else:
            rows.append({"field": name, "from": start, "to": end, "mode": mode})
    return rows


def rules_spec(rows):
    """The rows as the command line's ``--cloud-dates`` text (empty rows left out)."""
    return ";".join(f"{r['field']}|{r['from'].strip()}|{r['to'].strip()}|{r['mode']}"
                    for r in rows if r.get("field") and (r["from"].strip() or r["to"].strip()))


def candidate_counts(run_folder):
    """``{state: n}`` from a run's Memories index — what each scope would cover. ``{}`` if unknown."""
    path = os.path.join(run_folder, "Reports", "Memories", "data", "index.js")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        rows = json.loads(text[text.index("(") + 1:text.rindex(")")])
    except (OSError, ValueError):
        return {}
    counts = {}
    for row in rows:
        state = (row[5] or {}).get("cl") if len(row) > 5 else None
        if state:
            counts[state] = counts.get(state, 0) + 1
    return counts


def cli_values(values, rows, mode):
    """The window's answers in the command line's option names, for ``ui.build_request``."""
    scopes = [s for s in ("missing", "incomplete") if values.get(f"scope_{s}")]
    out = {"cloud": ",".join(scopes), "attest": "yes" if values.get("attest") else "",
           "authority": (values.get("authority") or "").strip(),
           "cloud-overlays": "yes" if values.get("overlays") else "no",
           "cloud-redownload": "yes" if values.get("redownload") else "",
           "cloud-delay": values.get("delay"), "cloud-jitter": values.get("jitter"),
           "cloud-max-per-min": values.get("maxpm"), "cloud-max-failures": values.get("maxfail"),
           "cloud-timeout": values.get("timeout"), "cloud-dates": rules_spec(rows)}
    if values.get("scope_selection") and (values.get("selection") or "").strip():
        out["cloud-selection"] = values["selection"].strip()
    if values.get("scope_snaps") and (values.get("snaps") or "").strip():
        out["cloud-snaps"] = values["snaps"]
    return {k: v for k, v in out.items() if v not in (None, "")}


# ------------------------------------------------------------------------------- the Cloud window

WARNING = ("This contacts Snapchat's servers. It requests only the addresses the device recorded for "
           "each Memory, with no account credentials, and decrypts what comes back with the key the "
           "device holds for that Memory. What comes back is NOT device evidence: it is kept in "
           "CloudDownloads/ apart from the extraction, and every report that shows it says so, with "
           "the authority you record below.")


def build_cloud_window(ui, *, mode, run_folder="", keychain="", pace=None):
    """The window. ``mode`` is ``run`` (configure a retrieval for the run about to start) or
    ``existing`` (retrieve for a run folder and refresh its reports)."""
    sg = ui.sg
    pace = pace or cloud_download.Pace()
    fields = cloud_memories.all_fields()
    hint = dict(font=ui.HINT_FONT, text_color=ui.hint_color())
    checks, row = [], []
    for i, name in enumerate(fields):
        row.append(sg.Checkbox(short_name(name), key=f"qf_{i}", font=ui.HINT_FONT, size=(24, 1),
                               tooltip=name))
        if len(row) == 3:
            checks.append(row)
            row = []
    if row:
        checks.append(row)
    rule_rows = []
    for i in range(MAX_RULES):
        rule_rows.append([sg.pin(sg.Column([[
            sg.Combo([short_name(f) for f in field_choices()], key=f"r{i}_field", readonly=True,
                     size=(24, 1)),
            sg.Text("from"), sg.Input("", key=f"r{i}_from", size=(17, 1)),
            sg.Text("to"), sg.Input("", key=f"r{i}_to", size=(17, 1)),
            sg.Combo(MODES, default_value="include", key=f"r{i}_mode", readonly=True, size=(8, 1)),
            sg.Button("Copy range to…", key=f"r{i}_copy"), sg.Button("✕", key=f"r{i}_del")]],
            key=f"r{i}_row", visible=False, pad=(0, 0)))])

    layout = [
        [sg.Text(WARNING, size=(96, 3), text_color="#0d4a75", background_color="#e3f1fb",
                 pad=((0, 0), (0, 8)))],
        [sg.Text("Legal authority", font=ui.SECTION_FONT),
         ui.help("Required before anything is requested, and asked again for every retrieval: the "
                 "authority belongs to a case, so it is never remembered. The note is recorded with "
                 "every request (CloudDownloads/cloud_manifest.jsonl) and printed wherever a "
                 "retrieved file is shown, on index.html, and on every page of a partial extract.",
                 title="Legal authority")],
        [sg.Checkbox("I hold the legal authority to retrieve this data from Snapchat's servers",
                     key="attest", enable_events=True)],
        [sg.Text("Authority", size=(10, 1)),
         sg.Input("", key="authority", enable_events=True, size=(100, 1))],
        [sg.Text("", size=(10, 1)),
         sg.Text("e.g. Search warrant #… issued by … on …  ·  Consent of <name>, <date>", **hint)],
    ]
    if mode == "existing":
        layout += [
            [sg.Text("Run folder", size=(10, 1)),
             sg.Input(run_folder, key="run_folder", enable_events=True, size=(90, 1)),
             sg.FolderBrowse(target="run_folder")],
            [sg.Text("", size=(10, 1)), sg.Text("", key="run_note", **hint)],
            [sg.Text("Keychain", size=(10, 1)), sg.Input(keychain, key="keychain", size=(90, 1)),
             sg.FileBrowse(target="keychain"),
             ui.help("Optional: the run records the keychain it was made with and uses it again. "
                     "Give one here only if that file has moved.", title="Keychain")],
        ]
    layout += [
        [sg.Text("What to retrieve", font=ui.SECTION_FONT, pad=((0, 0), (10, 2))),
         ui.help(cloud_memories.CANDIDATE_BASIS, title="What to retrieve")],
        [sg.Checkbox("Memories whose media is missing from the extraction", key="scope_missing",
                     default=True, enable_events=True), sg.Text("", key="n_missing", **hint)],
        [sg.Checkbox("Memories whose copy on the device is incomplete", key="scope_incomplete",
                     enable_events=True,
                     tooltip="partially cached, a video with only a still, only the transcoded "
                             "backup, or an overlay the row records that the device does not hold"),
         sg.Text("", key="n_incomplete", **hint)],
        [sg.Checkbox("Memories in a selection file", key="scope_selection", enable_events=True),
         sg.Input("", key="selection", size=(58, 1), enable_events=True),
         sg.FileBrowse(target="selection", file_types=(("Selection", "*.json *.js"),))],
        [sg.Checkbox("These snap ids — paste them from a Memory page or the index's «Copy snap "
                     "IDs»", key="scope_snaps", enable_events=True),
         sg.Text("", key="snaps_note", **hint)],
        [sg.Multiline("", key="snaps", size=(110, 3), enable_events=True)],
        [sg.Checkbox("Overlays too", key="overlays", default=True),
         sg.Checkbox("Retrieve again even if already retrieved", key="redownload")],
        [sg.Text("Date filter", font=ui.SECTION_FONT, pad=((0, 0), (10, 2))),
         ui.help("Applies to everything chosen above, pasted snap ids included. A Memory is "
                 "retrieved when at least one Include rule matches it (or there is none) and no "
                 "Exclude rule does. Dates are in the run's report timezone: YYYY-MM-DD or "
                 "YYYY-MM-DD HH:MM; a date alone covers that whole day; either end may be left "
                 "empty. To give several timestamps the same range, enter it once on the quick line, "
                 "tick the timestamps and press «Add for checked» — or use «Copy range to…» on a row.",
                 title="Date filter")],
        [sg.Text("Quick entry — from"), sg.Input("", key="q_from", size=(17, 1)),
         sg.Text("to"), sg.Input("", key="q_to", size=(17, 1)),
         sg.Combo(MODES, default_value="include", key="q_mode", readonly=True, size=(8, 1)),
         sg.Button("Add for checked", key="q_add"), sg.Button("All", key="q_all"),
         sg.Button("None", key="q_none")],
        [sg.Column(checks, pad=((20, 0), (0, 4)))],
    ] + rule_rows + [
        [sg.Button("+ Add rule", key="r_add"), sg.Text("", key="rules_note", **hint)],
        [sg.Text("Pace (adjustable while running)", font=ui.SECTION_FONT, pad=((0, 0), (10, 2))),
         ui.help("One request at a time. Between two requests: the delay plus a random 0–jitter "
                 "seconds, and never more than «max per minute» in any 60 seconds. On 429 or 5xx the "
                 "server's Retry-After is honoured, else the wait doubles from 30 s. It stops after "
                 "«stop after» failures in a row. All of this can be changed in the progress window "
                 "while it runs.", title="Pace")],
        [sg.Text("Delay"), sg.Input(str(pace.delay_s), key="delay", size=(5, 1)), sg.Text("s ±"),
         sg.Input(str(pace.jitter_s), key="jitter", size=(5, 1)), sg.Text("s   Max per minute"),
         sg.Input(str(pace.max_per_min), key="maxpm", size=(5, 1)), sg.Text("   Stop after"),
         sg.Input(str(pace.max_failures), key="maxfail", size=(4, 1)), sg.Text("failures   Timeout"),
         sg.Input(str(int(pace.timeout_s)), key="timeout", size=(5, 1)), sg.Text("s")],
    ]
    if mode == "existing":
        layout += [[sg.Text("Afterwards"),
                    sg.Radio("refresh the Memories and cache reports", "refresh", key="refresh_t",
                             default=True),
                    sg.Radio("re-render every report", "refresh", key="refresh_f")],
                   [sg.Text("Save your selections in the reports first (💾 Save selections): the "
                            "report pages are rewritten, and only saved selections survive.", **hint)]]
    layout += [[sg.Text("", key="problem", text_color="#b03535", size=(80, 2))],
                [sg.Push(), sg.Button("Start" if mode == "existing" else "Ok", key="start",
                                      disabled=True, size=(10, 1)),
                 sg.Button("Cancel", size=(10, 1))]]
    title = ("Snapchat Auto — retrieve Memories media from Snapchat's servers"
             + (" (during this run)" if mode == "run" else ""))
    window = sg.Window(title, [[sg.Column(layout, scrollable=True, vertical_scroll_only=True,
                                          expand_x=True, expand_y=True,
                                          size=ui.hidpi.px2((1000, 720)))]],
                       modal=True, resizable=True, finalize=True)
    window.set_min_size(ui.hidpi.px2((760, 420)))
    return window


def _rows_from(values, shown):
    rows = []
    fields = field_choices()
    by_short = {short_name(f): f for f in fields}
    for i in range(shown):
        name = by_short.get(values.get(f"r{i}_field") or "")
        rows.append({"field": name or "", "from": values.get(f"r{i}_from") or "",
                     "to": values.get(f"r{i}_to") or "", "mode": values.get(f"r{i}_mode") or "include"})
    return rows


def _show_rows(window, rows):
    for i in range(MAX_RULES):
        visible = i < len(rows)
        window[f"r{i}_row"].update(visible=visible)
        if visible:
            row = rows[i]
            window[f"r{i}_field"].update(value=short_name(row["field"]) if row["field"] else "")
            window[f"r{i}_from"].update(row["from"])
            window[f"r{i}_to"].update(row["to"])
            window[f"r{i}_mode"].update(value=row["mode"])


def _pick_fields(ui, title, preselect):
    """The checklist *Copy range to…* opens; returns the fields ticked, or None."""
    sg = ui.sg
    fields = cloud_memories.all_fields()
    layout = [[sg.Text(title)]] + [[sg.Checkbox(short_name(f), key=f, default=f in preselect)]
                                   for f in fields]
    layout += [[sg.Button("All"), sg.Button("None"), sg.Push(), sg.Button("Ok"), sg.Button("Cancel")]]
    window = sg.Window("Copy range to…", layout, modal=True, keep_on_top=True, finalize=True)
    try:
        while True:
            event, values = window.read()
            if event in (sg.WIN_CLOSED, "Cancel"):
                return None
            if event in ("All", "None"):
                for f in fields:
                    window[f].update(event == "All")
            if event == "Ok":
                return [f for f in fields if values.get(f)]
    finally:
        window.close()


def run_cloud_window(ui, *, mode, run_folder="", keychain="", tz="local", pace=None):
    """Show the window; return ``(request, extras)`` or ``(None, None)`` when cancelled.

    ``extras`` carries what only the existing-run mode asks: the run folder, a keychain, the refresh.
    """
    sg = ui.sg
    window = build_cloud_window(ui, mode=mode, run_folder=run_folder, keychain=keychain, pace=pace)
    shown, rows = 0, []
    fields = cloud_memories.all_fields()

    def refresh(values):
        counts = candidate_counts(values.get("run_folder") or "") if mode == "existing" else {}
        if mode == "existing":
            folder = values.get("run_folder") or ""
            ok = os.path.isdir(os.path.join(folder, "ExtractedData"))
            window["run_note"].update("" if ok else "not a Snapchat Auto run folder")
        for state in ("missing", "incomplete"):
            n = counts.get(state)
            window[f"n_{state}"].update(f"{n} Memory/Memories" if n is not None else "")
        ids, rejected = cloud_memories.snap_ids_from_text(values.get("snaps") or "")
        window["snaps_note"].update(f"{len(ids)} id(s)" + (f", {len(rejected)} not an id"
                                                           if rejected else ""))
        attested = bool(values.get("attest"))
        note_ok = len((values.get("authority") or "").strip()) >= cloud_download.MIN_NOTE
        window["start"].update(disabled=not (attested and note_ok))

    try:
        event, values = window.read(timeout=0)
        refresh(values)
        while True:
            event, values = window.read()
            if event in (sg.WIN_CLOSED, "Cancel"):
                return None, None
            if ui.handle_help(event):
                continue
            rows = _rows_from(values, shown)
            if event == "r_add" and shown < MAX_RULES:
                rows.append({"field": "", "from": "", "to": "", "mode": "include"})
            elif event == "q_add":
                picked = [f for i, f in enumerate(fields) if values.get(f"qf_{i}")]
                if picked:
                    rows = add_for_fields([r for r in rows if r["field"]], picked,
                                          values.get("q_from") or "", values.get("q_to") or "",
                                          values.get("q_mode") or "include")
            elif event in ("q_all", "q_none"):
                for i in range(len(fields)):
                    window[f"qf_{i}"].update(event == "q_all")
            elif event.endswith("_del") and event.startswith("r"):
                del rows[int(event[1:-4])]
            elif event.endswith("_copy") and event.startswith("r"):
                source = rows[int(event[1:-5])]
                have = {r["field"] for r in rows if r["mode"] == source["mode"]}
                picked = _pick_fields(ui, "Give these timestamps the same range and mode:",
                                      [f for f in fields if f not in have])
                if picked:
                    rows = add_for_fields(rows, picked, source["from"], source["to"], source["mode"])
            if len(rows) > MAX_RULES:
                rows = rows[:MAX_RULES]
                window["rules_note"].update(f"at most {MAX_RULES} rules")
            if rows != _rows_from(values, shown) or event == "r_add":
                shown = len(rows)
                _show_rows(window, rows)
            refresh(values)
            if event == "start":
                rule_rows = _rows_from(values, shown)
                request, error = ui.build_request(cli_values(values, rule_rows, mode),
                                                  "post-run" if mode == "existing" else "run", tz)
                if error:
                    window["problem"].update(error)
                    continue
                extras = {}
                if mode == "existing":
                    extras = {"run_folder": values.get("run_folder") or "",
                              "keychain": (values.get("keychain") or "").strip(),
                              "refresh": "full" if values.get("refresh_f") else "targeted"}
                return request, extras
    finally:
        window.close()


# ------------------------------------------------------------------------------ the progress window

def build_progress_window(ui, request):
    sg = ui.sg
    pace = request.pace
    hint = dict(font=ui.HINT_FONT, text_color=ui.hint_color())
    layout = [
        [sg.Text("Authority: " + request.authority.note, size=(90, 1), font=ui.SECTION_FONT)],
        [sg.Text(f"attested {request.authority.attested_utc} — retrieved media is not device "
                 "evidence", **hint)],
        [sg.ProgressBar(100, orientation="h", size=(60, 18), key="bar", expand_x=True)],
        [sg.Text("Starting…", key="counts", size=(90, 1))],
        [sg.Text("", key="now", size=(90, 1))],
        [sg.Multiline("", key="log", size=(100, 14), autoscroll=True, disabled=True,
                      expand_x=True, expand_y=True, font=("Consolas", 9))],
        [sg.Text("Delay"), sg.Input(str(pace.delay_s), key="delay", size=(5, 1)), sg.Text("s ±"),
         sg.Input(str(pace.jitter_s), key="jitter", size=(5, 1)), sg.Text("s   Max per minute"),
         sg.Input(str(pace.max_per_min), key="maxpm", size=(5, 1)), sg.Button("Apply", key="apply"),
         sg.Push(), sg.Button("Pause", key="pause", size=(9, 1)),
         sg.Button("Stop", key="stop", size=(9, 1)),
         sg.Button("Close", key="close", visible=False, size=(9, 1))],
    ]
    return sg.Window("Retrieving from Snapchat's servers", layout, resizable=True, finalize=True,
                     keep_on_top=False)


def _fmt_eta(seconds):
    if seconds is None:
        return ""
    if seconds < 90:
        return f"ETA ≈ {seconds:.0f} s"
    return f"ETA ≈ {seconds / 60:.0f} min"


def progress_runner(ui, request):
    """A ``CloudRequest.runner``: run the engine on a worker thread behind the progress window."""
    sg = ui.sg
    control = request.control = request.control or cloud_download.Control(request.pace)
    events = queue.Queue()

    def on_event(ev):
        events.put(ev)
        cloud_memories._log_event(ev)                      # the run log gets them too
    request.on_event = on_event

    def runner(run):
        window = build_progress_window(ui, request)
        result = {}

        def work():
            try:
                result["summary"] = run()
            except Exception as error:                       # noqa: BLE001 - shown, then raised
                result["error"] = error
            finally:
                events.put(None)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        done = False
        try:
            while True:
                event, values = window.read(timeout=100)
                if event in (sg.WIN_CLOSED, "close"):
                    if not done:
                        # Closing the window stops the retrieval — after the request in flight, which
                        # is recorded like every other — and waits for it, so the run carries on with
                        # a complete record rather than a thread still writing.
                        control.stop()
                        thread.join()
                    break
                if event == "pause":
                    if control.paused:
                        control.resume()
                        window["pause"].update("Pause")
                    else:
                        control.pause()
                        window["pause"].update("Resume")
                elif event == "stop":
                    control.stop()
                    window["now"].update("Stopping after the current request…")
                elif event == "apply":
                    try:
                        problems = control.update(delay_s=float(values["delay"]),
                                                  jitter_s=float(values["jitter"]),
                                                  max_per_min=int(values["maxpm"]))
                    except ValueError:
                        problems = ["not a number"]
                    window["now"].update("Pace: " + ("; ".join(problems) if problems else "applied"))
                while True:
                    try:
                        ev = events.get_nowait()
                    except queue.Empty:
                        break
                    if ev is None:
                        done = True
                        window["pause"].update(disabled=True)
                        window["stop"].update(disabled=True)
                        window["close"].update(visible=True)
                        summary = result.get("summary")
                        window["now"].update(
                            f"Finished: {summary.done} retrieved, {summary.failed} failed, "
                            f"{summary.skipped} already retrieved ({summary.stopped}). The reports "
                            "are written next — close this window." if summary else
                            f"Failed: {result.get('error')}")
                        continue
                    _show_event(window, ev)
        finally:
            window.close()
        if "error" in result:
            raise result["error"]
        return result.get("summary")
    return runner


def _show_event(window, ev):
    total = max(ev.total or 0, 1)
    finished = (ev.done or 0) + (ev.failed or 0) + (ev.skipped or 0)
    if ev.kind in ("start", "item_start", "item_done", "item_failed", "item_skipped", "end"):
        window["bar"].update(current_count=int(100 * finished / total))
        window["counts"].update(f"{finished} of {ev.total} — done {ev.done}, failed {ev.failed}, "
                                f"already retrieved {ev.skipped} · {ev.bytes / 1e6:.1f} MB"
                                + (f" · {_fmt_eta(ev.eta_s)}" if ev.eta_s else ""))
    if ev.kind == "wait":
        what = ev.job.snap_id if ev.job else ""
        window["now"].update(f"{what} waiting {ev.wait_s:.1f} s ({ev.text})" if ev.wait_s
                             else f"{ev.text}")
    elif ev.kind in ("item_start", "request"):
        window["now"].update(f"{ev.job.snap_id} {ev.job.role} — {ev.text or 'requesting'}")
    if ev.kind in ("item_done", "item_failed", "item_skipped", "log", "end"):
        who = f"{ev.job.snap_id} {ev.job.role}: " if ev.job else ""
        window["log"].update(f"{time.strftime('%H:%M:%S')} {who}{ev.text}\n", append=True)
