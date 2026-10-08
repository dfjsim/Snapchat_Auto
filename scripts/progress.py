"""Where a run is: its stages, the count inside the current one, a heartbeat, and a timing summary.

A run used to say what it was doing only between stages, and a stage on a large extraction can go
minutes without a line: decrypting thousands of cached files, hashing a tens-of-GB ZIP, cutting
thumbnails out of every cached video. The console then looks exactly like a crash. So the pipeline
reports here — cheaply, and without knowing who listens:

* :func:`stage` brackets a stage (``with progress.stage("Memories"): …``); stages nest.
* :func:`step` says where inside it the run is (*decrypting SCContent*, 340 of 1,320); a
  :class:`Counter` does the same from several threads at once.
* :func:`start_run` / :func:`finish_run` bracket a run: between them a **heartbeat** logs a
  *still working* line whenever nothing has been logged for :data:`HEARTBEAT_S` seconds, naming the
  stage and its count, and the end of the run logs how long each stage took — which is what says,
  in an examiner's log from a real case, where the time went.
* :func:`snapshot` is what the run window polls; :func:`request_skip` is how its *Skip* buttons
  reach the work (the thumbnail passes check :func:`skip_requested` between files).

Nothing here touches the reports, and with no run started every call is a cheap no-op beyond
updating a few attributes, so report modules used on their own — and the tests — are unaffected.
"""
import contextlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

#: Seconds without a log line after which the heartbeat says what the run is doing.
HEARTBEAT_S = 30.0

_lock = threading.Lock()


class _State:
    def __init__(self):
        self.started = time.monotonic()
        self.stack = []                 # open stages: [{"label", "start", "depth"}]
        self.done_stages = []           # closed stages: {"label", "seconds", "depth", "failed"}
        self.detail = ""
        self.done = None
        self.total = None
        self.last_activity = time.monotonic()
        self.skips = set()
        self.finished = False


_state = _State()
_heartbeat = None
_handler = None


class _ActivityHandler(logging.Handler):
    """Every log record is activity: the heartbeat speaks only into a silence."""

    def emit(self, record):
        _state.last_activity = time.monotonic()


def _install_handler():
    global _handler
    if _handler is None:
        _handler = _ActivityHandler(level=logging.DEBUG)
        logging.getLogger().addHandler(_handler)


# --------------------------------------------------------------------------------- the run

def start_run(heartbeat=True):
    """A run begins: forget the last one's stages and start the heartbeat."""
    global _state, _heartbeat
    with _lock:
        _state = _State()
    _install_handler()
    if heartbeat and (_heartbeat is None or not _heartbeat.is_alive()):
        _heartbeat = threading.Thread(target=_beat, name="progress-heartbeat", daemon=True)
        _heartbeat.start()


def finish_run():
    """A run ends: close what is still open, log the timing summary, stop the heartbeat.

    Called again for a run that already finished (the caller's ``finally``), it does nothing."""
    with _lock:
        if _state.finished:
            return list(_state.done_stages)
        while _state.stack:
            _close(_state.stack[-1], failed=True)
        _state.finished = True
        stages = list(_state.done_stages)
        total = time.monotonic() - _state.started
    if stages:
        logger.info(f"Stage timings (total {duration(total)}):")
        for s in stages:
            logger.info(f"  {'  ' * s['depth']}{s['label']}: {duration(s['seconds'])}"
                        + (" (stopped by an error)" if s["failed"] else ""))
    return stages


def _beat():
    while not _state.finished:
        time.sleep(1.0)
        quiet = time.monotonic() - _state.last_activity
        if quiet >= HEARTBEAT_S and _state.stack and not _state.finished:
            logger.info("… still working — " + describe())


# ------------------------------------------------------------------------------- stages

@contextlib.contextmanager
def stage(label):
    """Bracket one stage of the run. Nests; the timing summary indents nested stages."""
    entry = begin(label)
    failed = True
    try:
        yield entry
        failed = False
    finally:
        end(entry, failed=failed)


def begin(label):
    with _lock:
        entry = {"label": label, "start": time.monotonic(), "depth": len(_state.stack)}
        _state.stack.append(entry)
        _state.detail, _state.done, _state.total = "", None, None
    return entry


def end(entry, failed=False):
    with _lock:
        if entry in _state.stack:
            while _state.stack and _state.stack[-1] is not entry:
                _close(_state.stack[-1], failed=True)
            _close(entry, failed=failed)


def _close(entry, failed=False):
    _state.stack.remove(entry)
    _state.done_stages.append({"label": entry["label"], "depth": entry["depth"], "failed": failed,
                               "seconds": time.monotonic() - entry["start"]})
    _state.detail, _state.done, _state.total = "", None, None


def step(text=None, done=None, total=None):
    """Where inside the current stage the run is. Cheap: only attributes are set."""
    if text is not None:
        _state.detail = text
    _state.done = done
    _state.total = total


class Counter:
    """A count several threads advance together, shown as the current step."""

    def __init__(self, total, text):
        self.total, self.text, self.done = total, text, 0
        self._lock = threading.Lock()
        step(text, 0, total)

    def tick(self, n=1):
        with self._lock:
            self.done += n
            done = self.done
        step(self.text, done, self.total)


# ----------------------------------------------------------------------- what is happening

def describe():
    """One line: the stage, the step, the count, how long the stage has run."""
    with _lock:
        stack = list(_state.stack)
        detail, done, total = _state.detail, _state.done, _state.total
    if not stack:
        return "between stages"
    where = " › ".join(s["label"] for s in stack)
    if detail:
        where += f": {detail}"
    if done is not None and total:
        where += f" ({done:,} of {total:,})"
    elif done is not None:
        where += f" ({done:,})"
    return f"{where} — {duration(time.monotonic() - stack[-1]['start'])} in this stage"


def snapshot():
    """The state as plain data, for the run window."""
    now = time.monotonic()
    with _lock:
        return {"elapsed": now - _state.started,
                "quiet": now - _state.last_activity,
                "stack": [{"label": s["label"], "seconds": now - s["start"], "depth": s["depth"]}
                          for s in _state.stack],
                "done_stages": [dict(s) for s in _state.done_stages],
                "detail": _state.detail, "done": _state.done, "total": _state.total,
                "finished": _state.finished}


def duration(seconds):
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} min {sec:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


# ----------------------------------------------------------------------------------- skips

def request_skip(key):
    """Ask the work named ``key`` to stop where it is (the run window's Skip buttons)."""
    _state.skips.add(key)


def skip_requested(key):
    return key in _state.skips
