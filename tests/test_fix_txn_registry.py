"""The third review's fourth High: the transaction registry was keyed by `id(conn)` and never
cleaned up.

The comment claimed the registry "never grows during a run". It always grew: one lock and one depth
entry per connection the process had EVER opened, kept for the life of the process. Two costs, and
the second is the one that matters: a long-running process that opens and closes connections
accumulates stale entries, and CPython reuses object addresses, so a NEW connection can be handed
the bookkeeping of a dead one. A depth counter left above zero would make the new connection believe
it was already inside a transaction — it would never issue `BEGIN IMMEDIATE` and never commit.

The state is still per-connection (that is what makes nesting correct) and the lock is still held
for the whole outermost frame (that is what makes two threads on one connection serialise). What
changed is that the entry is dropped when the last thread leaves — so a stale entry cannot exist
for a connection nobody is using, and an id cannot be inherited.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from privatemesh import txn


def _entries() -> int:
    with txn._REGISTRY:
        return len(txn._LOCKS) + len(txn._DEPTHS)


def test_the_registry_is_empty_after_a_connection_is_used_and_dropped():
    """The finding in one test."""
    for _ in range(50):
        conn = sqlite3.connect(":memory:")
        with txn.transaction(conn):
            conn.execute("CREATE TABLE t(x)")
        conn.close()

    assert _entries() == 0, f"{_entries()} entries survived their connections"


def test_a_stale_depth_entry_cannot_be_inherited_by_a_new_connection():
    """The integrity half. Every frame closes at depth zero, so a new connection that lands on a
    recycled address starts at zero and opens its own transaction."""
    conn = sqlite3.connect(":memory:")
    with txn.transaction(conn):
        pass
    conn.close()

    assert txn.depth(conn) == 0
    assert _entries() == 0


def test_the_registry_does_not_grow_across_many_frames_on_one_connection():
    conn = sqlite3.connect(":memory:")
    for _ in range(25):
        with txn.transaction(conn):
            pass
    assert txn.depth(conn) == 0
    with txn._REGISTRY:
        assert len(txn._LOCKS) <= 1 and len(txn._DEPTHS) <= 1


# ================================================ the guarantees the registry exists for
def test_nesting_joins_the_outer_frame_and_commits_once():
    """The property the per-connection state is FOR. An inner frame that committed on its own would
    break the thing `_prepare` depends on: the version, the bundle and the plan are one commit."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")

    with pytest.raises(RuntimeError):
        with txn.transaction(conn):
            conn.execute("INSERT INTO t VALUES(1)")
            with txn.transaction(conn):
                conn.execute("INSERT INTO t VALUES(2)")
            raise RuntimeError("the outer frame failed")

    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    conn.close()


def test_two_threads_on_one_connection_do_not_enter_each_other_s_frame():
    """The property the LOCK is for. A depth counter keyed by connection alone lets a second thread
    count itself as an inner frame, write, return to its caller having committed nothing, and then
    be committed or rolled back by whatever the first thread did next. The control below is what
    proves the lock is doing it rather than luck of scheduling."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE t(x)")
    conn.commit()

    inside = threading.Event()
    release = threading.Event()
    overlapped: list[bool] = []

    def first():
        with txn.transaction(conn):
            inside.set()
            release.wait(2.0)
            conn.execute("INSERT INTO t VALUES(1)")
            time.sleep(0.05)

    def second():
        inside.wait(2.0)
        with txn.transaction(conn):
            # If this runs while `first` is still inside, the depth it saw was 1 and it believed
            # it was nested — so it returned without committing anything.
            overlapped.append(not release.is_set())
            conn.execute("INSERT INTO t VALUES(2)")

    one, two = threading.Thread(target=first), threading.Thread(target=second)
    one.start()
    two.start()
    time.sleep(0.1)
    release.set()
    one.join(5.0)
    two.join(5.0)

    assert not overlapped or overlapped == [False], "the second thread joined the first's frame"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    conn.close()


def test_an_exception_in_a_nested_frame_rolls_the_whole_thing_back():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(x)")
    with pytest.raises(ValueError):
        with txn.transaction(conn):
            conn.execute("INSERT INTO t VALUES(1)")
            with txn.transaction(conn):
                raise ValueError("inner")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    assert txn.depth(conn) == 0
    conn.close()
