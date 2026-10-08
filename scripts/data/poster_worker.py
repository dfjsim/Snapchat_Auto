"""Poster-frame extraction, in a process of its own so that it can be **stopped**.

Roughly one cached video in six cannot be decoded and hangs the decoder indefinitely — not slowly,
indefinitely: measured on a case extraction, a 57 KB cached MP4 held a single OpenCV ``read()`` for
over four minutes before the test was abandoned, and nothing in the file's structure predicts it
(the hanging files have a valid ``moov`` atom, and are truncated no more than the ones that decode
fine). So the only way to bound the work is to be able to kill it.

Killing needs a process. A thread cannot be stopped, and abandoning one is worse than waiting:

* it goes on decoding, so the pass gets slower as abandoned work piles up — measured on two case
  extractions, 630 videos took **20 minutes** and 590 took **82**;
* and it is inside :func:`scripts.data.ffmpeg_log.captured_stderr`, which redirects **file
  descriptor 2 for the whole process**. Two threads inside that at once interleave their
  ``dup``/``dup2``/``close`` calls, so fd 2 ends up on a descriptor that has been closed. The
  interpreter then cannot flush ``sys.stderr`` at shutdown, ``Py_FinalizeEx`` fails, and the process
  exits **120** — reports written, run reported as failed.

So: :func:`run_jobs` (the parent half) feeds one job at a time to each of a few subprocesses running
:func:`main` (the child half), and kills one that stops answering. The child announces each file
*before* it starts, so the parent knows which one to skip when it restarts. Nothing is abandoned.

Three things keep a large case from waiting on thumbnails:

* **Several workers** (:func:`default_workers`) decode at once; each is still one process with one
  file at a time, and still killed when that file hangs. A video's result does not depend on which
  worker took it, so only how long the pass takes changes.
* **A thumbnail cache** (:func:`set_cache_dir`, the run folder's ``.thumbnail_cache``): a frame is
  stored under the SHA-256 of the video it came from, so the same bytes are decoded once per run
  folder — not again by the next report that holds a copy, nor by a partial run or a refresh later.
  Only frames are cached; a hang is decided afresh by each run.
* **No time limit, and a Skip.** A pass runs until it is done. The run window's *Skip thumbnails*
  (``progress.request_skip(SKIP_KEY)``) stops it between files, and a headless run can set a limit
  (:func:`set_budget`, ``--thumbnail-minutes``). Either way a video not reached is **not attempted**
  — never reported as undecodable.
"""

import os
import sys
import time
import queue
import shutil
import hashlib
import logging
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

from scripts import progress

logger = logging.getLogger(__name__)

# The wall-clock bound on one video, enforced by killing its worker. A frame that comes out at all
# comes out in well under a second, so this is not a judgement about how long decoding takes — it is
# the line past which a file is not decoding at all, set with headroom for slow hardware.
FILE_TIMEOUT_S = 3.0
#: No limit on a whole pass by default: the run window can skip the rest instead (see the module doc).
BUDGET_S = None
#: At most this many workers, whatever the machine: each holds a decoder and its buffers.
MAX_WORKERS = 4
#: The name the run window's Skip button and the passes agree on.
SKIP_KEY = "thumbnails"

_settings = {"cache_dir": None, "budget": BUDGET_S, "workers": None}


def set_cache_dir(path):
    """Keep frames under ``path`` (a run folder's ``.thumbnail_cache``), or nowhere with None."""
    _settings["cache_dir"] = path


def set_budget(seconds):
    """A limit on each pass in seconds, for a headless run (``--thumbnail-minutes``); None for none."""
    _settings["budget"] = seconds


def set_workers(n):
    _settings["workers"] = n


def default_workers():
    """One worker per core but one, between 1 and :data:`MAX_WORKERS`."""
    return max(1, min(MAX_WORKERS, (os.cpu_count() or 2) - 1))


# --------------------------------------------------------------------------- parent half

def _worker_command():
    """How to start a second copy of this code as a process.

    From source that is ``python -m scripts.data.poster_worker``. In a packaged build there is no
    interpreter to run it with, so the application is re-entered with ``--poster-worker``, which
    `Snapchat_Auto.main` handles before it does anything else.

    Which of the two it is **cannot** be read off ``sys.executable``. A Nuitka standalone build —
    which is what the MSI installs, and what onefile unpacks — sets ``sys.executable`` to the
    *basename of the build machine's interpreter* joined onto the installation directory
    (``getStandaloneSysExecutablePath`` in Nuitka's ``CompiledCodeHelpers.c``). Built from a uv
    venv that is literally ``<install dir>\\python.exe``: a name that has never existed on the
    examiner's machine. Testing the basename therefore took the ``-m`` branch *in a build* and
    aimed the spawn at a phantom file, so every packaged run lost every thumbnail to
    ``[WinError 2]``. The reliable build marker is ``__compiled__``, which Nuitka pre-seeds into
    every compiled module (it sets neither ``sys.frozen`` nor ``sys._MEIPASS``); the interpreter is
    additionally required to *exist*, so a build Nuitka one day stops marking still cannot send the
    spawn to a file that is not there.
    """
    exe = sys.executable or ""
    if ("__compiled__" not in globals() and os.path.isfile(exe)
            and os.path.basename(exe).lower().startswith(("python", "pypy"))):
        return [exe, "-m", "scripts.data.poster_worker"], _package_root()
    return [_application_binary(), "--poster-worker"], None


def _application_binary():
    """This program's own executable, for a build to re-enter.

    Not ``sys.executable`` (see :func:`_worker_command`) — but its *directory* half is sound,
    because Nuitka builds it from the real binary directory. So: that directory, plus the name this
    program was invoked under. ``sys.argv[0]`` on its own would not do, because under onefile it is
    the outer launcher, and re-entering that would unpack the whole payload again for every single
    video; joining it onto the directory instead lands on the unpacked binary that is already
    running. ``realpath`` because Nuitka hands back the 8.3 short form of the directory
    (``…\\SNAPCH~1.DIS``), which works but is unreadable in a log or a process list.
    """
    name = os.path.basename(sys.argv[0] or "") or os.path.basename(sys.executable or "")
    if os.name == "nt" and not os.path.splitext(name)[1]:
        name += ".exe"
    binary = os.path.join(os.path.dirname(os.path.abspath(sys.executable or "")), name)
    if os.path.isfile(binary):
        return os.path.realpath(binary)
    return os.path.abspath(sys.argv[0] or name)


def _package_root():
    """The directory ``scripts.data.poster_worker`` is importable from."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _spawn():
    """Start the extraction subprocess."""
    command, cwd = _worker_command()
    return subprocess.Popen(
        command, cwd=cwd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # utf-8 on both ends (the child retunes its own streams in `main`). These pipes carry
        # FILE PATHS: under the locale default a path holding a character cp1252 cannot
        # represent was written as "?", so the worker was asked to open a file that does not
        # exist and the video came back as undecodable -- a claim about the evidence, made
        # from a mangled string. errors="replace" stays for stderr, which carries whatever
        # bytes ffmpeg emits.
        text=True, bufsize=1, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _drain(stream, sink):
    """Read a worker pipe to EOF into ``sink`` (a Queue or a list); ends when the pipe closes."""
    try:
        for line in stream:
            sink.put(line) if hasattr(sink, "put") else sink.append(line)
    except Exception:                                          # the pipe died with the worker
        pass


def _kill(proc):
    """Stop a worker and everything it is doing. Unlike a thread, it actually stops.

    Order matters: the process is killed **first**. Closing a pipe that a reader thread is blocked
    on deadlocks — ``close()`` wants the same buffer lock the blocked ``read()`` is holding. Killing
    the child closes its end, the read returns EOF, the reader thread finishes on its own, and only
    then is there nothing left to hold a lock.
    """
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        try:
            pipe.close()
        except Exception:
            pass


_DEFAULT = object()


def _drain_tagged(stream, sink, tag):
    """Like :func:`_drain`, tagging each line with the process it came from: the workers share one
    queue, and a line from a worker that has since been killed must not be read as its successor's."""
    try:
        for line in stream:
            sink.put((tag, line))
    except Exception:                                          # the pipe died with the worker
        pass


class _Slot:
    """One worker process, and the file it is on."""

    def __init__(self):
        self.proc, self.job, self.deadline = None, None, 0.0

    def start(self, lines, stderr_chunks):
        self.proc = _spawn()
        threading.Thread(target=_drain_tagged, args=(self.proc.stdout, lines, self.proc),
                         daemon=True).start()
        threading.Thread(target=_drain, args=(self.proc.stderr, stderr_chunks), daemon=True).start()

    def stop(self):
        _kill(self.proc)
        self.proc, self.job = None, None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _cache_keys(jobs, cache_dir):
    """``{src: key}`` — the video's SHA-256 and whether it is complete, which decides the frame."""
    if not cache_dir:
        return {}
    keys = {}

    def one(job):
        _key, src, _dst, complete = job
        try:
            return src, f"{_sha256(src)}-{'c' if complete else 'p'}"
        except OSError:
            return src, None
    with ThreadPoolExecutor(max_workers=4) as pool:          # reading, mostly: threads are enough
        for src, key in pool.map(one, jobs):
            if key:
                keys[src] = key
    return keys


def _copy(src, dst):
    try:
        shutil.copyfile(src, dst)
        return True
    except OSError:
        return False


def run_jobs(jobs, file_timeout=FILE_TIMEOUT_S, budget=_DEFAULT, workers=None):
    """Run ``[(src, dst, complete)]`` and return ``({src: True/False}, stderr chunks)``.

    A file the worker does not answer for within ``file_timeout`` is recorded as undecodable
    (``False``) and skipped; that worker is killed and a new one takes its place. The pass stops
    early only when skipped from the run window or when a ``budget`` (seconds) runs out — by default
    there is none — and then whatever is left is not attempted.

    A key is **absent** from the result when that file was never attempted — no worker could be
    started, one could not run at all, or the pass was stopped first. Callers must keep that apart
    from ``False``: "this video did not decode" is a finding about the evidence, and it may not be
    written next to a file this tool never opened.
    """
    # Resolve here, in the parent, where the working directory is the run folder: the worker runs
    # from the package root so that `-m` can find it, so a relative path would mean a different file
    # there — or, as it did, no file at all and a silent zero posters.
    jobs = [(src, os.path.abspath(src), os.path.abspath(dst), complete)
            for src, dst, complete in jobs]
    budget = _settings["budget"] if budget is _DEFAULT else budget
    results, stderr_chunks = {}, []
    total, reused = len(jobs), 0
    cache_dir = _settings["cache_dir"]
    keys = _cache_keys(jobs, cache_dir)

    # what the cache already holds, and the same bytes asked for twice in one pass: decoded once
    pending, followers = [], {}
    for job in jobs:
        key, _src, dst, _complete = job
        ck = keys.get(job[1])
        if ck and _copy(os.path.join(cache_dir, ck + ".jpg"), dst):
            results[key] = True
            reused += 1
            continue
        if ck and ck in followers:
            followers[ck].append(job)
            continue
        if ck:
            followers[ck] = []
        pending.append(job)
    n_workers = min(workers or _settings["workers"] or default_workers(), len(pending)) or 1
    if pending:
        logger.info(f"Thumbnails: {len(pending)} video(s) to decode with {n_workers} worker(s)"
                    + (f"; {reused} frame(s) reused from the thumbnail cache" if reused else ""))

    lines = queue.Queue()
    slots = [_Slot() for _ in range(n_workers)]
    queue_ = list(reversed(pending))                           # popped from the end, in order
    deadline = time.monotonic() + budget if budget else None
    killed, stopped, fatal = 0, False, False
    tries = {}                                                 # src -> failed hand-overs

    def answered(slot, ok):
        results[slot.job[0]] = ok
        slot.job = None
        progress.step("cutting thumbnails from cached video", len(results), total)

    try:
        while True:
            if not stopped and (fatal or progress.skip_requested(SKIP_KEY)
                                or (deadline and time.monotonic() >= deadline)):
                stopped = True
            for slot in slots:                                 # hand out work
                if stopped or slot.job is not None or not queue_:
                    continue
                if slot.proc is None:
                    try:
                        slot.start(lines, stderr_chunks)
                    except Exception as error:
                        logger.warning(f"Poster frames unavailable: the extraction worker could not "
                                       f"be started ({error}) — {len(queue_)} video(s) will be "
                                       f"listed without a thumbnail, and are NOT reported as "
                                       f"undecodable")
                        stopped = fatal = True
                        break
                job = queue_.pop()
                try:
                    slot.proc.stdin.write(f"{job[1]}\t{job[2]}\t{1 if job[3] else 0}\n")
                    slot.proc.stdin.flush()
                except OSError:                                # the worker died on its own
                    slot.stop()
                    tries[job[0]] = tries.get(job[0], 0) + 1
                    if tries[job[0]] < 3:
                        queue_.append(job)                     # a fresh worker takes it
                    continue
                slot.job, slot.deadline = job, time.monotonic() + file_timeout
            busy = [slot for slot in slots if slot.job is not None]
            if not busy and (stopped or not queue_):
                break
            # wait for the first answer, or for the earliest file to run out of time
            now = time.monotonic()
            wait = max(0.01, min([slot.deadline for slot in busy], default=now + 0.1) - now)
            try:
                proc, line = lines.get(timeout=wait)
            except queue.Empty:
                proc, line = None, ""
            line = line.strip()
            for slot in busy:
                if proc is not None and slot.proc is proc and slot.job is not None:
                    if line.startswith("OK ") or line.startswith("NO "):
                        answered(slot, line.startswith("OK "))
                    elif line.startswith("FATAL "):
                        logger.warning(f"Poster frames unavailable: the extraction worker could not "
                                       f"start ({line[6:]}) — the videos are listed without a "
                                       f"thumbnail, which says nothing about whether they decode")
                        slot.job = None                        # not attempted
                        stopped = fatal = True
                if slot.job is not None and time.monotonic() >= slot.deadline:
                    killed += 1
                    answered(slot, False)                      # attempted, and it hung: undecodable
                    slot.stop()                                # a fresh worker takes the next one
    finally:
        for slot in slots:
            slot.stop()

    # the frames the cache did not have yet, and the copies asked for twice
    for job in pending:
        key, src, dst, _complete = job
        ck = keys.get(src)
        if not ck:
            continue
        if results.get(key) and cache_dir:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                target = os.path.join(cache_dir, ck + ".jpg")
                if not os.path.exists(target):
                    shutil.copyfile(dst, target + ".tmp")
                    os.replace(target + ".tmp", target)
            except OSError:
                pass
        for follower in followers.get(ck, ()):
            if key not in results:
                continue                                       # not attempted: nor are its copies
            results[follower[0]] = bool(results[key]) and _copy(dst, follower[2])
    if killed:
        logger.debug(f"{killed} video(s) blocked the decoder and were skipped")
    left = total - len(results)
    if left and not fatal:
        why = ("skipped from the run window" if progress.skip_requested(SKIP_KEY)
               else f"stopped after {budget / 60:.0f} min" if budget else "stopped")
        logger.info(f"Thumbnails: {why} with {left} video(s) not attempted — they are listed "
                    f"without a thumbnail")
    return results, stderr_chunks


# --------------------------------------------------------------------------- child half

def main(argv=None):
    # cv2's logging is set here rather than by the parent: this process is the only one that loads
    # it, and the variables are read at import time.
    for var, value in (("OPENCV_LOG_LEVEL", "OFF"), ("OPENCV_FFMPEG_LOGLEVEL", "0"),
                       ("OPENCV_VIDEOIO_DEBUG", "0")):
        os.environ.setdefault(var, value)
    try:
        from scripts.data.poster_frame import generate_poster
    except Exception as error:                                 # pragma: no cover - import guard
        sys.stdout.write(f"FATAL {error}\n")
        sys.stdout.flush()
        return 1

    # The other end of the same agreement: without this the child decodes the paths the
    # parent sent, and encodes its own echo, with the locale encoding.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):                   # not a retunable stream
            pass

    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        src, dst = parts[0], parts[1] if len(parts) > 1 else ""
        complete = len(parts) > 2 and parts[2] == "1"
        # Announced BEFORE it runs: if this process is killed for hanging, that line is the last
        # thing the parent saw, so it knows exactly which file to skip on the retry.
        sys.stdout.write(f"START {src}\n")
        sys.stdout.flush()
        try:
            # no stderr capture: this process's stderr belongs to the parent, which captures and
            # summarises it. A per-call fd-2 redirect here would take that away.
            ok = generate_poster(src, dst, complete=complete)
        except Exception:
            ok = False
        sys.stdout.write(f"{'OK' if ok else 'NO'} {src}\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
