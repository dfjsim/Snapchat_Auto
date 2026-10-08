"""Doing independent per-file work on several threads, without changing what a report says.

The reports read, hash and decode every file of a cache one after another. Most of that time is
waiting on the disk — on Windows, on the antivirus scanning a file the extraction has just written —
and hashing, which ``hashlib`` does outside the GIL; both overlap well on threads. What must not change
is the **order** the results are used in: the reports merge, name and de-duplicate in walk order
(first copy wins), and a different order would be a different report. So :func:`ordered_map` runs the
work on threads and hands the results back strictly in input order — the caller's loop stays exactly
as it was, it only stops waiting on the read.

Only work that is independent per item goes through here: reading and hashing a file, decoding its
bytes, reading its metadata. Anything that writes shared state, publishes a file or decides a name
stays in the caller's loop.
"""
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor

#: Threads for I/O-bound per-file work: enough to keep a disk and an antivirus scanner busy.
MAX_THREADS = 8


def default_threads():
    return max(2, min(MAX_THREADS, (os.cpu_count() or 2)))


def ordered_map(fn, items, threads=None, window=None):
    """``fn(item)`` for every item, on several threads, yielded **in input order**.

    At most ``window`` items (default four per thread) are in flight or waiting to be consumed, so a
    slow item early on never lets the rest pile up in memory — a cache file can be hundreds of MB. An
    exception raised by ``fn`` is raised here, at that item's place in the order, exactly as the
    sequential loop would have raised it.
    """
    threads = threads or default_threads()
    window = window or threads * 4
    if threads <= 1:
        for item in items:
            yield fn(item)
        return
    pending = deque()
    it = iter(items)
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for item in it:
            pending.append(pool.submit(fn, item))
            if len(pending) >= window:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()
