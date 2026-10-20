"""MED 5 — two writers on one audit file must produce one valid chain.

The old write path protected chain-head selection with `self._lock`, which is per `AuditLog`
OBJECT. Two instances (or two processes) over the same SQLite file each held their own lock, so
both could read the same tip and both try to insert the same next id — one losing to a UNIQUE
conflict, after the skill it was receipting had already run.

The fix takes the database's write lock with `BEGIN IMMEDIATE` before reading the head, so the
read and the insert are one atomic step, and retries briefly if the lock is held.
"""
from __future__ import annotations


import threading

from privatemesh.audit import AuditLog


def _hammer(path: str, *, instances: int, threads: int, each: int):
    """Interleave writes from several `AuditLog` objects over one file. Returns (logs, errors)."""
    logs = [AuditLog(path) for _ in range(instances)]
    errors: list[BaseException] = []
    start = threading.Barrier(threads)

    def work(k: int) -> None:
        start.wait()
        try:
            for i in range(each):
                logs[k % instances].record("evt", task_id=f"t{k}", detail={"i": i})
        except BaseException as exc:            # noqa: BLE001 - the point is to catch everything
            errors.append(exc)

    workers = [threading.Thread(target=work, args=(k,)) for k in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return logs, errors


def test_concurrent_writers_on_one_file_produce_one_valid_chain(tmp_path):
    path = str(tmp_path / "audit.db")
    logs, errors = _hammer(path, instances=2, threads=6, each=15)

    assert errors == [], f"a concurrent writer failed: {errors[:3]}"
    rows = logs[0].rows()
    assert len(rows) == 90
    # No id was handed out twice and none was skipped: exactly one chain, not two interleaved.
    assert [r["id"] for r in rows] == list(range(1, 91))
    assert logs[0].verify().ok is True
    assert logs[1].verify().ok is True


def test_a_second_instance_can_write_while_the_first_holds_the_write_lock(tmp_path):
    """The retry path: a writer that arrives while the lock is held waits and then succeeds,
    rather than failing an action that has already been taken."""
    path = str(tmp_path / "audit.db")
    holder = AuditLog(path)
    holder.record("first")

    holder._conn.execute("BEGIN IMMEDIATE")          # simulate a writer mid-transaction
    outcome: dict = {}

    def late_writer() -> None:
        try:
            outcome["row"] = AuditLog(path).record("second")
        except BaseException as exc:                # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=late_writer)
    thread.start()
    thread.join(timeout=1.0)
    assert thread.is_alive(), "the writer did not wait for the lock — it should block, not fail"
    holder._conn.execute("COMMIT")                  # release
    thread.join(timeout=5.0)

    assert "error" not in outcome, outcome.get("error")
    assert outcome["row"]["id"] == 2
    assert holder.verify().ok is True


def test_a_genuinely_unavailable_store_is_still_loud(tmp_path):
    """Retrying a busy lock must not turn a broken store into a silent one."""
    import pytest

    from privatemesh.audit import AuditError

    log = AuditLog(str(tmp_path / "audit.db"))
    log.record("ok")
    log._conn.close()
    with pytest.raises(AuditError, match="audit write failed"):
        log.record("will_fail")


def test_the_connection_waits_for_a_locked_database(tmp_path):
    """The busy timeout is set on the connection, so SQLite's own wait and the retry loop agree
    about how long a writer is willing to wait."""
    log = AuditLog(str(tmp_path / "audit.db"))
    timeout = log._conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert int(timeout) >= 5000


def test_ids_are_still_assigned_explicitly_not_by_autoincrement(tmp_path):
    """The chained hash commits to the id, so the id cannot be handed out after the hash is
    computed — that is why the id is chosen inside the transaction rather than by SQLite."""
    log = AuditLog(str(tmp_path / "audit.db"))
    first = log.record("a")
    second = log.record("b")
    assert (first["id"], second["id"]) == (1, 2)
    assert second["prev_hash"] == first["hash"]
    # The id is part of the hashed payload, so the stored digest only verifies if the id was
    # fixed before the hash was taken — recomputing it from the row must reproduce the row.
    from privatemesh.audit import row_hash

    row = log.rows()[1]
    payload = {k: row[k] for k in ("id", "ts", "task_id", "event", "detail")}
    assert row_hash(row["prev_hash"], payload) == row["hash"]
