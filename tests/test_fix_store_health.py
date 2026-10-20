"""The third review's persistence Mediums and the Low that sits beside them.

Three of them are the same shape: the store knows something the owner is not told.

  * `corrupt` was filled once, by the constructor's `load()`. A row made unreadable LATER — by
    another process, or by a database swapped underneath this one — left the session reporting
    `corrupt: 0` while the store held a record it could not read.
  * Migration turned a malformed legacy payload into `version=0, revision=0` and said nothing, so an
    unreadable record became an apparently valid one that a later save would overwrite — losing the
    original evidence and replacing it with state derived from nothing.
  * `load()` ran once, at construction, while `store_health()` reads the store's live row count. A
    store that changed underneath the process reported a count that its own answers disagreed with.

Each fix is the same move: say what is true about the store rather than what was true when the
process started.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from privatemesh import changes as ch


def _store(tmp_path):
    return ch.ChangeStore(path=str(tmp_path / "changes.db"))


def _change(change_id: str = "c1") -> ch.Change:
    return ch.Change(id=change_id, thread_id="t1", message_id="m1", from_name="Alex Doe",
                     from_address="alex@example.com", sender_pinned="alex@example.com",
                     subject="Lunch", received_at="2026-09-18T09:00:00+01:00",
                     excerpt="could we move it", stage=ch.READY)


def _write_raw(path, change_id: str, payload: str) -> None:
    """Another process, writing a row this build cannot read."""
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO changes(id, payload, updated_at, version, revision) "
                 "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                 (change_id, payload, "", 0, 0))
    conn.commit()
    conn.close()


# ================================================ the count is about the store, not the process
def test_health_notices_a_record_made_unreadable_by_another_process(tmp_path):
    store = _store(tmp_path)
    store.save(_change())
    assert store.counts()["corrupt"] == 0

    _write_raw(tmp_path / "changes.db", "c2", "{not json at all")

    assert store.counts()["records"] == 2
    assert store.counts()["corrupt"] == 1, (
        "the session reported a clean store while the store held an unreadable record")


def test_health_does_not_double_count_a_record_it_already_reported(tmp_path):
    """A re-scan replaces the answer rather than appending to it. A count that only ever grows is
    not a count."""
    store = _store(tmp_path)
    _write_raw(tmp_path / "changes.db", "c2", "{broken")
    assert store.counts()["corrupt"] == 1
    assert store.counts()["corrupt"] == 1
    assert store.counts()["corrupt"] == 1


def test_a_record_that_becomes_readable_again_stops_being_reported(tmp_path):
    """The mirror image, and the reason a re-scan REPLACES: a row repaired by another process — or
    by a build that learned to read it — is not corrupt any more."""
    store = _store(tmp_path)
    _write_raw(tmp_path / "changes.db", "c2", "{broken")
    assert store.counts()["corrupt"] == 1

    _write_raw(tmp_path / "changes.db", "c2", ch._dump_change(_change("c2")))
    assert store.counts()["corrupt"] == 0


def test_the_steady_state_does_not_re_parse_the_whole_store(tmp_path):
    """The re-scan is bounded by a change in the row count, not by a clock. A session payload is
    fetched on every screen; parsing every record each time would make it a per-render cost."""
    store = _store(tmp_path)
    store.save(_change())
    store.counts()
    scans = []
    real = store._rescan
    store._rescan = lambda: (scans.append(1), real())[1]

    for _ in range(10):
        store.counts()
    assert scans == [], "the store re-read every record on a call that could not have changed"


# ================================================ migration does not launder a bad row
def test_a_malformed_legacy_row_is_reported_rather_than_silently_zeroed(tmp_path):
    """The Medium. The migration's job is to add the columns; it is not licensed to decide that an
    unreadable payload describes a valid record."""
    path = tmp_path / "changes.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE changes(id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO changes(id, payload) VALUES('legacy', '{not json')")
    conn.commit()
    conn.close()

    store = ch.ChangeStore(path=str(path))

    health = store.counts()
    assert health["records"] == 1
    assert health["corrupt"] == 1, "a malformed legacy row migrated into an apparently valid one"


def test_a_readable_legacy_row_migrates_with_its_own_version(tmp_path):
    """The control: the migration still does its job for the rows it can read."""
    path = tmp_path / "changes.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE changes(id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL DEFAULT '')")
    change = _change("legacy")
    change.version, change.revision = 4, 7
    conn.execute("INSERT INTO changes(id, payload) VALUES('legacy', ?)", (ch._dump_change(change),))
    conn.commit()
    conn.close()

    store = ch.ChangeStore(path=str(path))
    assert store.counts() == {"records": 1, "corrupt": 0}
    assert store.load()[0].version == 4


def test_saving_over_a_record_this_build_cannot_read_is_refused(tmp_path):
    """The consequence the Medium names: a later save with the same id replaces the evidence with
    state derived from nothing. Refusing is the only answer that keeps the original row."""
    store = _store(tmp_path)
    _write_raw(tmp_path / "changes.db", "c1", "{not json")

    with pytest.raises(ch.UnreadableRecord):
        store.save(_change("c1"))

    assert store.counts()["corrupt"] == 1, "the unreadable row was overwritten anyway"


def test_saving_a_new_record_is_unaffected(tmp_path):
    store = _store(tmp_path)
    store.save(_change("c1"))
    store.save(_change("c2"))
    assert store.counts() == {"records": 2, "corrupt": 0}


# ================================================ the Low: the answer says which store it is about
def test_the_session_reports_the_records_it_actually_serves(tmp_path):
    """The Low. `store_health()` read the store's live row count while the engine served the set it
    loaded at startup, so the two could disagree with nothing saying so."""
    from test_fix_change_persistence import harness, prepared

    h = harness(tmp_path)
    prepared(h)
    health = h.v2().store_health()

    assert health["records"] == 1
    assert health["loaded"] == 1
    assert health["corrupt"] == 0

    _write_raw(tmp_path / "changes.db", "elsewhere", json.dumps({"id": "elsewhere"}))
    health = h.v2().store_health()
    assert health["records"] == 2
    assert health["loaded"] == 1, "the divergence between the file and the engine is not visible"
