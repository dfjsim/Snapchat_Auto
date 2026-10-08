"""One poster frame out of one video — the work a thumbnail worker process does.

Kept apart from the report modules so the worker (:mod:`scripts.data.poster_worker`) imports OpenCV and
nothing else: a worker is started once per worker slot and again after every video that hangs the
decoder, and importing a whole report module each time was most of what a restart cost.
``memories_media_report.generate_poster`` is this function with that report's stderr capture.
"""
import contextlib
import logging
import os

logger = logging.getLogger(__name__)

# How many frames to try before giving up on a poster. Partially cached video decodes at the start
# and fails after that, so the frame we want is always within the first few reads; the bound is
# what stops a badly damaged file from being decoded end to end for a thumbnail.
POSTER_MAX_READS = 60


def first_decodable(cap, limit):
    """The first frame that decodes, reading forward from the current position. None if none does."""
    for _ in range(limit):
        ok, frame = cap.read()
        if ok and frame is not None:
            return frame
        if not ok:                                         # stream ended / unrecoverable
            return None
    return None


def generate_poster(video_path, out_path, at_seconds=1.0, complete=True, quiet_ctx=None):
    """Extract a single poster frame from a video into out_path (JPEG). Returns True on success.

    The result is a DERIVED artifact (not original device data) — callers must label it as such.

    Incompletely cached video still gets a poster. What the cache holds starts at the beginning of
    the file, so the opening frames decode even when the sample table points past the bytes on
    disk; for those files the seek is skipped (seeking into missing bytes fails, and the failure
    costs a full re-read) and the first frame that decodes is taken.

    **This call can block forever** — see :mod:`scripts.data.poster_worker`. ``quiet_ctx``, when
    given, is a context manager to run the decoder inside (the reports pass one that captures
    FFmpeg's stderr); it redirects fd 2 for the whole process, so it is not for concurrent use.
    """
    for var in ("OPENCV_LOG_LEVEL", "OPENCV_FFMPEG_LOGLEVEL", "OPENCV_VIDEOIO_DEBUG"):
        os.environ.setdefault(var, "OFF" if "LOG_LEVEL" in var else "0")
    try:
        import cv2
    except Exception as error:
        logger.debug(f"cv2 unavailable, cannot generate poster: {error}")
        return False
    try:
        frame = None
        with (quiet_ctx() if quiet_ctx else contextlib.nullcontext()):
            cap = cv2.VideoCapture(video_path)
            try:
                if complete:
                    fps = cap.get(cv2.CAP_PROP_FPS) or 0
                    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                    if fps and frames:
                        cap.set(cv2.CAP_PROP_POS_FRAMES,
                                min(int(fps * at_seconds), max(int(frames) - 1, 0)))
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        frame = first_decodable(cap, POSTER_MAX_READS)
                else:
                    frame = first_decodable(cap, POSTER_MAX_READS)
            finally:
                cap.release()
        if frame is None:
            return False
        # imencode + a plain write, not cv2.imwrite: on Windows imwrite goes through the ANSI
        # API, so a destination path holding any character outside the system codepage makes
        # it return False and write nothing -- the poster is lost with no error, for a run
        # whose only sin was a case folder with an accent in it.
        ok, buffer = cv2.imencode(os.path.splitext(out_path)[1] or ".jpg", frame)
        if not ok:
            return False
        with open(out_path, "wb") as fh:
            fh.write(buffer.tobytes())
        return True
    except Exception as error:
        logger.debug(f"poster generation failed for {video_path}: {error}")
        return False
