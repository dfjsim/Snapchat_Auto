"""A staged copy of an evidence database is removed even when it will not open.

``sqlite_open.open_views`` without a workdir stages its two readings in a temporary folder that
``Views.close`` removes. A connection whose first read fails must be closed there and then: a
``sqlite3.Connection`` is in a reference cycle with its statement cache, so left to the garbage
collector it keeps the staged file open, and on Windows the folder — a copy of the evidence — would
be left behind. The garbage collector is switched off here so the result cannot depend on when it
happens to run. Every input is synthetic.
"""
import gc
import glob
import os
import sqlite3
import tempfile

import pytest

from scripts.data import sqlite_open

STAGED = os.path.join(tempfile.gettempdir(), "scauto_sqlite_*")


@pytest.fixture
def no_gc():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def _garbage(path):
    with open(path, "wb") as fh:
        fh.write(b"\x5a" * 8192)


def _wal_beside_garbage(folder):
    """A main file that will not open beside a valid ``-wal``: the merged reading may open, the
    checkpointed one cannot."""
    good = os.path.join(folder, "good.db")
    conn = sqlite3.connect(good)
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma wal_autocheckpoint=0")
    conn.execute("create table t (a)")
    conn.execute("insert into t values (1)")
    conn.commit()
    with open(good + "-wal", "rb") as fh:                     # before close() checkpoints it away
        wal = fh.read()
    conn.close()
    evidence = os.path.join(folder, "evidence.db")
    _garbage(evidence)
    with open(evidence + "-wal", "wb") as fh:
        fh.write(wal)
    return evidence


def test_a_failed_open_does_not_hold_the_file(tmp_path, no_gc):
    path = str(tmp_path / "not_a_database.db")
    _garbage(path)

    assert sqlite_open._open_ro(path) is None
    os.remove(path)                                           # nothing holds it open
    assert not os.path.exists(path)


@pytest.mark.parametrize("shape", ["not a database", "-wal beside a main file that will not open"])
def test_no_staged_copy_is_left_when_a_reading_will_not_open(tmp_path, no_gc, shape):
    if shape == "not a database":
        path = str(tmp_path / "evidence.db")
        _garbage(path)
    else:
        path = _wal_beside_garbage(str(tmp_path))
    before = set(glob.glob(STAGED))

    views = sqlite_open.open_views(path)
    assert views.main_only is None                            # the reading that failed
    views.close()

    assert set(glob.glob(STAGED)) <= before
