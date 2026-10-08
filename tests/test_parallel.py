"""Per-file work on several threads, handed back in the order a report merges it in.

Every input is synthetic.
"""
import threading
import time

import pytest

from scripts import memories_media_report as mr
from scripts import parallel


def test_results_come_back_in_input_order_whatever_finishes_first():
    def slow_first(n):
        time.sleep(0.05 if n == 0 else 0)
        return n * n
    assert list(parallel.ordered_map(slow_first, range(20), threads=4)) == [n * n for n in range(20)]


def test_the_work_really_is_shared_out_and_bounded():
    seen, lock, running, peak = set(), threading.Lock(), [0], [0]

    def work(n):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            seen.add(threading.get_ident())
        time.sleep(0.01)
        with lock:
            running[0] -= 1
        return n
    out = list(parallel.ordered_map(work, range(40), threads=4, window=6))
    assert out == list(range(40)) and len(seen) > 1 and peak[0] <= 4


def test_an_error_is_raised_at_its_place_in_the_order():
    def fails_on_three(n):
        if n == 3:
            raise ValueError("three")
        return n
    got = []
    with pytest.raises(ValueError):
        for value in parallel.ordered_map(fails_on_three, range(10), threads=3):
            got.append(value)
    assert got == [0, 1, 2]                      # what came before it, exactly as a loop would give


def test_one_thread_is_the_plain_loop():
    assert list(parallel.ordered_map(str, [1, 2], threads=1)) == ["1", "2"]


def test_the_sccontent_listing_is_kept_until_a_folder_changes(tmp_path):
    folder = tmp_path / "Documents" / "com.snap.file_manager_3_SCContent_x"
    folder.mkdir(parents=True)
    (folder / ("a" * 32)).write_bytes(b"1")
    (folder / (("b" * 32) + "_0-9")).write_bytes(b"2")
    full, parts = mr.index_sccontent(str(tmp_path))
    assert list(full) == ["a" * 32] and list(parts) == ["b" * 32]
    full["a" * 32].append("mine")                # a caller's copy is its own
    again, _ = mr.index_sccontent(str(tmp_path))
    assert again["a" * 32] == [str(folder / ("a" * 32))]
    time.sleep(0.02)
    (folder / ("c" * 32)).write_bytes(b"3")      # the folder changed: listed afresh
    assert "c" * 32 in mr.index_sccontent(str(tmp_path))[0]
