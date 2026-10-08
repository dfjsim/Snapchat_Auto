"""The run window: where a run is, while it runs — the GUI's view of :mod:`scripts.progress`.

The GUI used to close its window and leave the examiner with the console, which went quiet for
minutes at a time on a large extraction. Now the run happens on a worker thread behind a window that
shows, a few times a second:

* the stages done, with how long each took, and the one running, with the step and count inside it
  (``progress.snapshot()``), a bar when the count has a total, and how long since anything was logged
  — so a slow stage is visibly a slow stage, not a crash;
* the end of the log, with a count of warnings and errors (a logging handler feeding a queue);
* while a retrieval from Snapchat's servers runs, its progress, the request in flight, the pace and
  Pause / Resume / Stop — the same controls as its own window had, which a run started from this
  window no longer opens (:func:`cloud_runner`);
* *Skip thumbnails* while thumbnails are being cut (``progress.request_skip``), and at the end
  *Open report* and *Open folder*.

Only this thread touches the window. The pipeline on the worker thread never does: what it says
reaches here through the progress module, the log queue and the cloud event queue.
"""
import logging
import os
import queue
import threading
import time
import webbrowser

from scripts import progress

#: Refresh interval of the window, in milliseconds.
TICK_MS = 250
#: Log lines kept on screen (the log file has them all).
LOG_LINES = 600
#: After this many seconds without a log line, the window says how long it has been.
QUIET_S = 10


class _QueueLog(logging.Handler):
    """Every log record, formatted, into a queue the window drains."""

    def __init__(self, sink, formatter=None):
        super().__init__(level=logging.INFO)
        self.sink = sink
        if formatter:
            self.setFormatter(formatter)

    def emit(self, record):
        try:
            self.sink.put((record.levelno, self.format(record)))
        except Exception:                                      # noqa: BLE001 - never break the run
            pass


# ------------------------------------------------------------------------------- what to show

def stage_text(snap):
    """The stages as lines: done ones with their time, the open ones with where they are."""
    lines = []
    for s in snap["done_stages"]:
        mark = "✗" if s["failed"] else "✓"
        lines.append(f"{'   ' * s['depth']}{mark} {s['label']} — {progress.duration(s['seconds'])}")
    for s in snap["stack"]:
        lines.append(f"{'   ' * s['depth']}▶ {s['label']} — {progress.duration(s['seconds'])}")
    return "\n".join(lines)


def step_text(snap):
    if not snap["stack"]:
        return "" if snap["finished"] else "Starting…"
    text = snap["detail"] or snap["stack"][-1]["label"]
    if snap["done"] is not None and snap["total"]:
        text += f" — {snap['done']:,} of {snap['total']:,}"
    elif snap["done"] is not None:
        text += f" — {snap['done']:,}"
    return text


def quiet_text(snap):
    if snap["finished"] or snap["quiet"] < QUIET_S:
        return ""
    return (f"Still working — nothing logged for {progress.duration(snap['quiet'])}. "
            "A long step can be silent for minutes; the count above says it is moving.")


# ------------------------------------------------------------------- the retrieval, in this window

def cloud_runner(request, events):
    """A ``CloudRequest.runner`` for a run inside this window.

    The engine runs on the calling thread — the run's worker thread — and every event goes to
    ``events`` for the window (and to the log, as ever). The window drives ``request.control``, which
    the engine reads between requests.
    """
    from scripts import cloud_download, cloud_memories
    request.control = request.control or cloud_download.Control(request.pace)

    def on_event(ev):
        events.put(ev)
        cloud_memories._log_event(ev)
    request.on_event = on_event

    def runner(run):
        events.put("begin")
        try:
            return run()
        finally:
            events.put("end")
    return runner


def _cloud_rows(ui, request):
    sg = ui.sg
    pace = request.pace
    hint = dict(font=ui.HINT_FONT, text_color=ui.hint_color())
    return [
        [sg.Text("Retrieving from Snapchat's servers", font=ui.SECTION_FONT)],
        [sg.Text("Authority: " + request.authority.note, **hint)],
        [sg.ProgressBar(100, orientation="h", size=(60, 14), key="c_bar", expand_x=True)],
        [sg.Text("Starting…", key="c_counts", size=(100, 1))],
        [sg.Text("", key="c_now", size=(100, 1))],
        [sg.Text("Delay"), sg.Input(str(pace.delay_s), key="c_delay", size=(5, 1)), sg.Text("s ±"),
         sg.Input(str(pace.jitter_s), key="c_jitter", size=(5, 1)), sg.Text("s   Max per minute"),
         sg.Input(str(pace.max_per_min), key="c_maxpm", size=(5, 1)),
         sg.Button("Apply", key="c_apply"), sg.Push(),
         sg.Button("Pause", key="c_pause", size=(9, 1)), sg.Button("Stop", key="c_stop", size=(9, 1))],
    ]


def _show_cloud(window, ev):
    total = max(ev.total or 0, 1)
    finished = (ev.done or 0) + (ev.failed or 0) + (ev.skipped or 0)
    if ev.kind in ("start", "item_start", "item_done", "item_failed", "item_skipped", "end"):
        window["c_bar"].update(current_count=int(100 * finished / total))
        window["c_counts"].update(f"{finished} of {ev.total} — retrieved {ev.done}, failed "
                                  f"{ev.failed}, already retrieved {ev.skipped} · "
                                  f"{ev.bytes / 1e6:.1f} MB")
    if ev.kind == "wait":
        what = ev.job.snap_id if ev.job else ""
        window["c_now"].update(f"{what} waiting {ev.wait_s:.1f} s ({ev.text})" if ev.wait_s
                               else ev.text)
    elif ev.kind in ("item_start", "request"):
        window["c_now"].update(f"{ev.job.snap_id} {ev.job.role} — {ev.text or 'requesting'}")


# ------------------------------------------------------------------------------------ the window

def build_window(ui, title, cloud_request=None):
    sg = ui.sg
    hint = dict(font=ui.HINT_FONT, text_color=ui.hint_color())
    mono = ("Consolas", 9)
    cloud = ([[sg.pin(sg.Column(_cloud_rows(ui, cloud_request), key="cloud_panel", visible=False,
                                expand_x=True))]] if cloud_request is not None else [])
    layout = [
        [sg.Text(title, font=ui.SECTION_FONT), sg.Push(), sg.Text("", key="elapsed")],
        [sg.Multiline("", key="stages", size=(100, 10), disabled=True, font=mono, expand_x=True,
                      no_scrollbar=False)],
        [sg.Text("Starting…", key="step", size=(100, 1))],
        [sg.ProgressBar(1000, orientation="h", size=(60, 14), key="bar", expand_x=True)],
        [sg.Text("", key="quiet", size=(100, 1), **hint)],
        *cloud,
        [sg.Text("Log", font=ui.SECTION_FONT), sg.Push(), sg.Text("", key="counts", **hint)],
        [sg.Multiline("", key="log", size=(110, 14), autoscroll=True, disabled=True, font=mono,
                      expand_x=True, expand_y=True)],
        [sg.Button("Skip thumbnails", key="skip", disabled=True,
                   tooltip="Stop cutting thumbnails out of cached video for the rest of this run. "
                           "Videos not reached are listed as not attempted, never as undecodable."),
         sg.Push(), sg.Button("Open report", key="open", disabled=True),
         sg.Button("Open folder", key="folder", disabled=True), sg.Button("Close", key="close")],
    ]
    # the X asks first, like Close, instead of destroying a window the run is still reporting into
    return sg.Window(title, layout, resizable=True, finalize=True,
                     enable_close_attempted_event=True)


def run_in_window(ui, run_fn, kwargs, *, title="Snapchat Auto — processing", formatter=None,
                  auto_close=False):
    """Run ``run_fn(**kwargs)`` on a worker thread behind the run window; return ``(result, error)``.

    ``error`` is whatever the run raised, :class:`SystemExit` included (some stages end a run that
    way), for the caller to explain. Closing the window while the run is going asks first, then ends
    the program: a pipeline cannot be stopped half-way and left in a state worth continuing from.
    ``auto_close`` closes the window as soon as the run ends (for tests).
    """
    sg = ui.sg
    logs, cloud_events = queue.Queue(), queue.Queue()
    cloud_request = kwargs.get("cloud")
    if cloud_request is not None:
        cloud_request.runner = cloud_runner(cloud_request, cloud_events)
    handler = _QueueLog(logs, formatter)
    root = logging.getLogger()
    root.addHandler(handler)
    window = build_window(ui, title, cloud_request)
    outcome, finished = {}, threading.Event()

    def work():
        try:
            outcome["result"] = run_fn(**kwargs)
        except BaseException as error:                         # noqa: BLE001 - reported to the window
            outcome["error"] = error
        finally:
            finished.set()

    worker = threading.Thread(target=work, name="snapchat-auto-run", daemon=True)
    worker.start()
    n_warn = n_err = 0
    shown_stages = None
    skipped = done_shown = False
    try:
        while True:
            event, values = window.read(timeout=TICK_MS)
            if event == sg.WIN_CLOSED:                         # gone without asking: nothing to show on
                break
            if event in (sg.WINDOW_CLOSE_ATTEMPTED_EVENT, "close"):
                if finished.is_set():
                    break
                if sg.popup_yes_no("The run is still going. Close the window and stop it? The "
                                   "reports would be incomplete.", title="Stop the run?",
                                   keep_on_top=True) == "Yes":
                    logging.getLogger(__name__).warning("Run stopped by the examiner (window closed)")
                    for h in root.handlers:
                        try:
                            h.flush()
                        except Exception:                      # noqa: BLE001
                            pass
                    os._exit(1)
                continue
            if event == "skip":
                progress.request_skip("thumbnails")
                skipped = True
                window["skip"].update("Thumbnails skipped", disabled=True)
            elif event in ("open", "folder"):
                folder = outcome.get("result") or ""
                target = os.path.join(folder, "index.html") if event == "open" else folder
                if os.path.exists(target):
                    if event == "open":
                        webbrowser.open("file:///" + os.path.abspath(target).replace("\\", "/"))
                    elif hasattr(os, "startfile"):
                        os.startfile(target)                   # noqa: S606 - the examiner's folder
            elif event and event.startswith("c_") and cloud_request is not None:
                control = cloud_request.control
                if event == "c_pause":
                    if control.paused:
                        control.resume()
                        window["c_pause"].update("Pause")
                    else:
                        control.pause()
                        window["c_pause"].update("Resume")
                elif event == "c_stop":
                    control.stop()
                    window["c_now"].update("Stopping after the current request…")
                elif event == "c_apply":
                    try:
                        problems = control.update(delay_s=float(values["c_delay"]),
                                                  jitter_s=float(values["c_jitter"]),
                                                  max_per_min=int(values["c_maxpm"]))
                    except ValueError:
                        problems = ["not a number"]
                    window["c_now"].update("Pace: " + ("; ".join(problems) if problems
                                                       else "applied"))

            # what the run is doing
            snap = progress.snapshot()
            text = stage_text(snap)
            if text != shown_stages:
                window["stages"].update(text)
                shown_stages = text
            window["elapsed"].update(progress.duration(snap["elapsed"]))
            window["step"].update(step_text(snap))
            if snap["total"]:
                window["bar"].update(current_count=int(1000 * min(snap["done"] or 0, snap["total"])
                                                       / snap["total"]))
            window["quiet"].update(quiet_text(snap))
            cutting = (snap["detail"] or "").startswith("cutting thumbnails")
            if not skipped:
                window["skip"].update(disabled=not cutting)

            # the log
            batch = []
            while True:
                try:
                    level, line = logs.get_nowait()
                except queue.Empty:
                    break
                batch.append(line)
                n_warn += level == logging.WARNING
                n_err += level >= logging.ERROR
            if batch:
                window["log"].update("\n".join(batch[-LOG_LINES:]) + "\n", append=True)
                window["counts"].update(f"{n_warn} warning(s), {n_err} error(s)")

            # the retrieval
            while cloud_request is not None:
                try:
                    ev = cloud_events.get_nowait()
                except queue.Empty:
                    break
                if ev == "begin":
                    window["cloud_panel"].update(visible=True)
                elif ev == "end":
                    for key in ("c_pause", "c_stop", "c_apply"):
                        window[key].update(disabled=True)
                    window["c_now"].update("Finished — the run continues with the reports.")
                else:
                    _show_cloud(window, ev)

            if finished.is_set() and not done_shown:
                done_shown = True
                error = outcome.get("error")
                window["skip"].update(disabled=True)
                if error is None:
                    window["step"].update(f"Finished in {progress.duration(snap['elapsed'])}.")
                    window["open"].update(disabled=False)
                    window["folder"].update(disabled=False)
                else:
                    window["step"].update(f"The run stopped: {_describe(error)}")
                    window["folder"].update(disabled=not outcome.get("result"))
                if auto_close:
                    break
    finally:
        root.removeHandler(handler)
        window.close()
    worker.join(timeout=1)
    return outcome.get("result"), outcome.get("error")


def _describe(error):
    if isinstance(error, SystemExit):
        return f"it ended itself (exit code {error.code}) — see the log"
    return f"{type(error).__name__}: {error}"
