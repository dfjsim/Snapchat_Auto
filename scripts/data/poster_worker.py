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

So: :func:`run_jobs` (the parent half) feeds one job at a time to a subprocess running
:func:`main` (the child half), and kills it if it stops answering. The child announces each file
*before* it starts, so the parent knows which one to skip when it restarts. Nothing is abandoned.
"""

import os
import sys
import time
import queue
import logging
import threading
import subprocess

logger = logging.getLogger(__name__)

# Wall-clock bounds on one video and on the whole pass, both enforced by killing the worker. A frame
# that comes out at all comes out in well under a second, so the per-file bound is not a judgement
# about how long decoding takes — it is the line past which a file is not decoding at all, set with
# headroom for slow hardware. The budget is the backstop for a case where much of the cached video
# is undecodable; reaching it costs nothing but thumbnails, and what it did not reach is reported.
FILE_TIMEOUT_S = 3.0
BUDGET_S = 600.0


# --------------------------------------------------------------------------- parent half

def _worker_command():
    """How to start a second copy of this code as a process.

    From source that is ``python -m scripts.data.poster_worker``. In a packaged build there is no
    interpreter to run it with — ``sys.executable`` is the application itself — so the application
    is re-entered with ``--poster-worker``, which `Snapchat_Auto.main` handles before it does
    anything else. Getting this wrong in a build would launch a GUI per cached video, so the test is
    on what ``sys.executable`` actually is rather than on a frozen-build marker (Nuitka sets neither
    ``sys.frozen`` nor ``sys._MEIPASS``).
    """
    exe = os.path.basename(sys.executable or "").lower()
    if exe.startswith("python") or exe.startswith("pypy"):
        return [sys.executable, "-m", "scripts.data.poster_worker"], _package_root()
    return [sys.executable, "--poster-worker"], None


def _package_root():
    """The directory ``scripts.data.poster_worker`` is importable from."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _spawn():
    """Start the extraction subprocess."""
    command, cwd = _worker_command()
    return subprocess.Popen(
        command, cwd=cwd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _drain(stream, sink):
    """Read a worker pipe to EOF into ``sink`` (a Queue or a list); ends when the pipe closes."""
    try:
        for line in stream:
            sink.put(line) if hasattr(sink, "put") else sink.append(line)
    except Exception:                                          # the pipe died with the worker
        pass


def _await_result(lines, timeout):
    """True/False for the current file, or None when the worker stopped answering in time."""
    end = time.monotonic() + timeout
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = lines.get(timeout=remaining).strip()
        except queue.Empty:
            return None
        if line.startswith("START "):                          # reached the file, still working
            continue
        if line.startswith("OK ") or line.startswith("NO "):
            return line.startswith("OK ")
        if line.startswith("FATAL "):
            logger.info(f"Poster frames unavailable: {line[6:]}")
            return None


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


def run_jobs(jobs, file_timeout=FILE_TIMEOUT_S, budget=BUDGET_S):
    """Run ``[(src, dst, complete)]`` and return ``({src: True/False}, stderr chunks)``.

    A file the worker does not answer for within ``file_timeout`` is recorded as undecodable and
    skipped; the worker is killed and a new one takes over from the next file. The whole pass stops
    at ``budget``, whatever is left undone — a thumbnail is a convenience, and no convenience may
    cost the examiner their report.
    """
    # Resolve here, in the parent, where the working directory is the run folder: the worker runs
    # from the package root so that `-m` can find it, so a relative path would mean a different file
    # there — or, as it did, no file at all and a silent zero posters.
    jobs = [(src, os.path.abspath(src), os.path.abspath(dst), complete)
            for src, dst, complete in jobs]
    results, stderr_chunks = {}, []
    index, deadline, killed = 0, time.monotonic() + budget, 0
    while index < len(jobs) and time.monotonic() < deadline:
        try:
            proc = _spawn()
        except Exception as error:
            logger.info(f"Poster frames unavailable ({error}) — video will be listed without a "
                        f"thumbnail")
            return results, stderr_chunks
        lines = queue.Queue()
        threading.Thread(target=_drain, args=(proc.stdout, lines), daemon=True).start()
        threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True).start()
        try:
            while index < len(jobs) and time.monotonic() < deadline:
                key, src, dst, complete = jobs[index]
                try:
                    proc.stdin.write(f"{src}\t{dst}\t{1 if complete else 0}\n")
                    proc.stdin.flush()
                except OSError:
                    break                                      # the worker died on its own
                answered = _await_result(
                    lines, min(file_timeout, max(0.1, deadline - time.monotonic())))
                index += 1                                     # dealt with, either way
                if answered is None:
                    killed += 1
                    break                                      # hung: kill it, start a new one
                results[key] = answered                        # keyed as the caller passed it
        finally:
            _kill(proc)
    if killed:
        logger.debug(f"{killed} video(s) blocked the decoder and were skipped")
    if index < len(jobs):
        logger.info(f"Poster frames: stopped after {budget:.0f}s with {len(jobs) - index} video(s) "
                    f"not attempted — they are listed without a thumbnail")
    return results, stderr_chunks


# --------------------------------------------------------------------------- child half

def main(argv=None):
    # cv2's logging is set here rather than by the parent: this process is the only one that loads
    # it, and the variables are read at import time.
    for var, value in (("OPENCV_LOG_LEVEL", "OFF"), ("OPENCV_FFMPEG_LOGLEVEL", "0"),
                       ("OPENCV_VIDEOIO_DEBUG", "0")):
        os.environ.setdefault(var, value)
    try:
        from scripts.memories_media_report import generate_poster
    except Exception as error:                                 # pragma: no cover - import guard
        sys.stdout.write(f"FATAL {error}\n")
        sys.stdout.flush()
        return 1

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
            # quiet=False: this process's stderr belongs to the parent, which captures and
            # summarises it. A per-call fd-2 redirect here would take that away.
            ok = generate_poster(src, dst, complete=complete, quiet=False)
        except Exception:
            ok = False
        sys.stdout.write(f"{'OK' if ok else 'NO'} {src}\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
