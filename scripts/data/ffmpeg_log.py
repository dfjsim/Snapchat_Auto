"""Capture FFmpeg's decoder chatter and report it through ``logging`` instead of the console.

OpenCV's FFmpeg writes to **file descriptor 2 from C**, so ``contextlib.redirect_stderr`` never
sees it and the ``OPENCV_FFMPEG_*`` environment variables do not help either: those options reach
the demuxer, while ``Invalid NAL unit size`` and ``missing picture in access unit`` come from the
decoder context. The result was messages that appeared on the console but **never in the run's
``.log`` file** — the one place an examiner looks afterwards.

Discarding them is not the right fix either. "moov atom not found" on a cached video is a
*finding*: it means the device only ever cached part of that file. So fd 2 is redirected to a
temporary file for the duration of the call, and what FFmpeg wrote is summarised into the log with
an explanation of what it implies, per ``docs/forensics_tool_guidelines.md`` ("show the user if
something is not available because of ... 0-byte files, etc").
"""

import os
import re
import sys
import logging
import tempfile
import contextlib
import collections

logger = logging.getLogger(__name__)

# What each decoder complaint means for the file it was raised on. Anything unmatched is counted
# under "other" and the first example is logged verbatim, so a new message is never swallowed.
MESSAGE_MEANINGS = [
    (re.compile(r"moov atom not found", re.I),
     "no movie header — the cache holds only part of the file (the moov atom is written last), "
     "or the bytes are not really an MP4"),
    (re.compile(r"Invalid NAL unit size|Error splitting the input into NAL units", re.I),
     "the H.264 stream is truncated mid-frame — normal for a partially cached video"),
    (re.compile(r"missing picture in access unit", re.I),
     "a frame's data is incomplete — normal for a partially cached video"),
    (re.compile(r"partial file", re.I),
     "the file ends before the container says it should — only part of it was cached"),
    (re.compile(r"Invalid data found when processing input", re.I),
     "the bytes are not decodable as the container claims"),
]


@contextlib.contextmanager
def captured_stderr(sink=None):
    """Redirect OS-level stderr for the block; append whatever was written to ``sink`` (a list).

    Falls back to doing nothing when there is no real fd 2 (pythonw, some frozen hosts), so a
    caller never has to guard against it.
    """
    try:
        sys.stderr.flush()
    except Exception:                                          # pragma: no cover - detached stderr
        pass
    try:
        saved = os.dup(2)
    except (OSError, ValueError, AttributeError):
        yield
        return
    handle = None
    path = None
    try:
        handle, path = tempfile.mkstemp(prefix="scauto_ffmpeg_")
        os.dup2(handle, 2)
        yield
    finally:
        try:
            os.dup2(saved, 2)
        finally:
            os.close(saved)
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
            if path:
                try:
                    if sink is not None:
                        with open(path, "r", encoding="utf-8", errors="replace") as fh:
                            text = fh.read()
                        if text.strip():
                            sink.append(text)
                    os.unlink(path)
                except OSError:
                    pass


def summarise(chunks):
    """Count the decoder messages in captured output. Returns ``{meaning: count}`` plus examples."""
    counts = collections.Counter()
    examples = {}
    other = []
    for chunk in chunks or []:
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            for pattern, meaning in MESSAGE_MEANINGS:
                if pattern.search(line):
                    counts[meaning] += 1
                    examples.setdefault(meaning, line)
                    break
            else:
                if line.startswith("[") and len(other) < 3:
                    other.append(line)
                if line.startswith("["):
                    counts["other decoder message"] += 1
    return counts, examples, other


def log_summary(chunks, context, log=None):
    """Log one summary line per distinct decoder complaint. Returns the total message count.

    ``context`` names what was being processed ("poster-frame extraction", say) so the log says
    which step produced the messages.
    """
    log = log or logger
    counts, examples, other = summarise(chunks)
    if not counts:
        return 0
    total = sum(counts.values())
    log.info(f"FFmpeg reported {total} decoder message(s) during {context}. These come from the "
             f"media itself, not from a failure of this tool:")
    for meaning, count in counts.most_common():
        if meaning == "other decoder message":
            for line in other:
                log.info(f"  {count}x other: {line}")
            continue
        log.info(f"  {count}x {meaning}")
    return total
