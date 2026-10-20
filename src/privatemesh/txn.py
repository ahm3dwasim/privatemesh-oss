"""One write transaction, shared by the stores that must commit together.

The review's High finding is that a `threading.Lock` protects one object in one process: two
processes can load the same approval, mutate their own copy, and commit last-writer-wins, and a
crash between two related writes leaves the durable state describing a plan that never existed.
Neither is fixable in Python — the lock that matters is the database's.

So writes take a real SQLite transaction, opened with `BEGIN IMMEDIATE` rather than the implicit
deferred one. The difference is the whole point: a deferred transaction only takes the write lock
at the first write, so two processes can both read, both decide they may write, and the loser
finds out after it has already reasoned about a row that moved. `BEGIN IMMEDIATE` takes the write
lock at the start, so the second writer blocks until the first commits and then sees its result.

Nesting is by connection: a store that is called from inside another store's transaction joins it
rather than opening a second one, and only the outermost frame commits. That is what makes "the
change's version and the change-set it points at commit together" a property of the code rather
than of the order two calls happen to be written in.

Two threads sharing one connection are a different problem from two processes sharing one file, and
this module answers it with a lock rather than with the depth counter. A depth counter keyed by
connection ALONE would let a second thread enter while the first was inside, count itself as an
inner frame, write, return to its caller having "committed" nothing — and then be committed or
rolled back by whatever the first thread did next. The per-connection `RLock` is held for the whole
outermost frame, so the second thread waits for the first and then opens its own transaction. It is
re-entrant because the nesting above is re-entrant: the same thread must be able to enter its own
frame from a nested store, and an `RLock` lets it while still excluding everybody else.

The lock is keyed by `id(conn)` because `sqlite3.Connection` is a C type: it does not accept
attributes and cannot be weakly referenced, so there is nowhere to hang per-connection state except
a table keyed by identity. That table is the hazard the third review named. It used to be permanent
— one lock and one depth entry per connection the process had EVER opened, kept forever, while the
comment claimed it "never grows during a run". Two costs, and the second is the one that matters:
`id()` is an address, and CPython reuses addresses, so a new connection could be handed the
bookkeeping of a dead one. A depth left above zero would make that new connection believe it was
already inside a transaction — it would never issue `BEGIN IMMEDIATE` and never commit.

So an entry now lives exactly as long as someone is using it. It is created on the way in, and
dropped on the way out once the frame count is back to zero AND no other thread is holding or
waiting on the lock. The waiters are what make the drop safe: a thread that has already looked the
lock up and not yet acquired it is counted, so the entry cannot be dropped out from under it and
replaced by a second lock for the same connection. A connection nobody is inside has no entry, so
there is no stale entry to inherit.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading

_DEPTHS: dict[int, int] = {}
_LOCKS: dict[int, threading.RLock] = {}
#: Threads that hold or are waiting for a connection's lock. Entries may only be dropped at zero.
_USERS: dict[int, int] = {}
_REGISTRY = threading.Lock()


@contextlib.contextmanager
def _claim(conn: sqlite3.Connection):
    """The connection's lock, held for the duration of the `with` block, with the entry dropped on
    the way out when nobody else can still need it."""
    key = id(conn)
    with _REGISTRY:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        _USERS[key] = _USERS.get(key, 0) + 1
    try:
        with lock:
            yield lock
    finally:
        with _REGISTRY:
            _USERS[key] -= 1
            if _USERS[key] == 0:
                _USERS.pop(key, None)
                # Only ever reached with the frame count at zero: a thread inside a frame still
                # holds the lock, so it cannot be here. What remains is the last thread out.
                if _DEPTHS.get(key, 0) == 0:
                    _LOCKS.pop(key, None)
                    _DEPTHS.pop(key, None)


def depth(conn: sqlite3.Connection) -> int:
    """How many `transaction()` frames are open on this connection, in this thread."""
    with _REGISTRY:
        return _DEPTHS.get(id(conn), 0)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    """A write transaction on `conn`, nested and thread-safe.

    The outermost frame issues `BEGIN IMMEDIATE` and commits on a clean exit or rolls back on any
    exception — including `BaseException`, because a `KeyboardInterrupt` in the middle of a
    two-store write is exactly the crash this exists to make atomic. An inner frame does nothing
    but count, so a store called from inside another store's transaction cannot commit half of it.

    The connection's lock is taken for the whole frame, so the count is only ever read and written
    by the one thread that holds it. A second thread blocks rather than joining.
    """
    with _claim(conn):
        current = _DEPTHS.get(id(conn), 0)
        if current == 0:
            conn.execute("BEGIN IMMEDIATE")
        _DEPTHS[id(conn)] = current + 1
        try:
            yield conn
        except BaseException:
            _DEPTHS[id(conn)] = current
            if current == 0:
                conn.rollback()
            raise
        _DEPTHS[id(conn)] = current
        if current == 0:
            conn.commit()
