"""The third review's second and third Highs: two writes that must not be able to disagree.

The edit path called `changesets.edit()` and then `_persist(change)` with no transaction around
them, so a crash between the two left a durable change-set version n+1 that the change record —
still on version n — does not point at. And the broker's approval rows are raised BEFORE the
changes/change_sets transaction begins, so a failure in that transaction left approvals on disk
with no committed plan: authorization state discoverable and consumable on its own.

Both are about the same thing, which is why they are one file. The fix for the first is the
transaction `_prepare` already used; the fix for the second is that a plan that did not commit
leaves no live approval behind. The broker calls stay OUTSIDE the transaction on purpose — they do
provider I/O, and a write lock held across them would block every other writer — so what the code
owes is a compensation, not a wider lock.
"""
from __future__ import annotations

import pytest

from privatemesh import changes as ch

from fakes_v2 import change_email
from test_fix_change_persistence import harness, prepared


def _approvals(h) -> dict:
    return {a.id: a for a in h.approvals.list(status="pending")}


def _versions(h, change_id: str) -> list[int]:
    cs = h.changesets.for_change(change_id)
    if cs is None:
        return []
    rows = h.changesets._conn.execute(
        f"SELECT version FROM {h.changesets._table} WHERE id=? ORDER BY version", (cs.id,)
    ).fetchall()
    return [int(r["version"]) for r in rows]


# ================================================ the edit is one commit
def test_a_failed_edit_leaves_no_change_set_version_the_record_does_not_point_at(tmp_path):
    """The finding, exactly: crash between `changesets.edit` and `_persist`."""
    h = harness(tmp_path)
    change = prepared(h)
    before = _versions(h, change.id)
    assert before == [1]

    def explode(_change):
        raise RuntimeError("the process died between the two writes")

    h.engine.store.save = explode
    with pytest.raises(RuntimeError):
        h.engine.revise(change.id, reply_body="Hi Alex, 15:00 works.")

    assert _versions(h, change.id) == before, (
        "a version the change record does not reference survived the failure")
    assert h.engine.require(change.id).version == 1


def test_a_successful_edit_commits_the_record_and_the_version_together(tmp_path):
    """The other half: the transaction must not be so wide that it rolls back a good edit."""
    h = harness(tmp_path)
    change = prepared(h)
    edited = h.engine.revise(change.id, reply_body="Hi Alex, 15:00 works.")

    assert edited.version == 2
    assert _versions(h, change.id) == [1, 2]
    after = harness(tmp_path)
    assert after.engine.get(change.id).version == 2


def test_a_failed_edit_invalidates_the_approvals_the_broker_raised_for_it(tmp_path):
    """An edit raises NEW approvals over the NEW arguments. If the edit did not commit, those
    approvals authorize a version that does not exist."""
    h = harness(tmp_path)
    change = prepared(h)
    prepared_ids = set(_approvals(h))

    h.engine.store.save = _raise
    with pytest.raises(RuntimeError):
        h.engine.revise(change.id, reply_body="Hi Alex, 15:00 works.")

    orphans = set(_approvals(h)) - prepared_ids
    assert not orphans, f"approvals left pending for an edit that never committed: {orphans}"


# ================================================ approvals do not outlive a failed plan
def test_a_failed_prepare_leaves_no_orphan_approval(tmp_path):
    """The third High: broker calls raise approval rows before the ATTACH transaction begins."""
    h = harness(tmp_path, messages=[change_email("m1")])
    change = h.engine.detect(None)[0]
    assert _approvals(h) == {}

    h.engine.store.save = _raise
    with pytest.raises(RuntimeError):
        h.engine.prepare(change.id)

    assert _approvals(h) == {}, (
        "a pending approval survived a plan that was never committed")


def test_the_compensation_does_not_reach_approvals_it_did_not_raise(tmp_path):
    """The control. `_approvals` is not "clear the store" — an approval raised by an unrelated
    change, or an earlier version of this one, must be left alone."""
    h = harness(tmp_path)
    change = prepared(h)
    untouched = h.approvals.request(task_id="other", tool="email_reply", arguments={})

    h.engine.store.save = _raise
    with pytest.raises(RuntimeError):
        h.engine.revise(change.id, reply_body="Hi Alex, 15:00 works.")

    assert untouched.id in _approvals(h), "an unrelated approval was invalidated"


def test_a_prepare_that_fails_before_the_broker_raises_anything_still_refuses(tmp_path):
    """A failure BEFORE the broker runs has nothing to compensate, and the transaction must still
    leave no half-written record behind."""
    h = harness(tmp_path)
    change = h.engine.detect(None)[0]

    original = h.changesets.create

    def refuse(**_kw):
        raise RuntimeError("the bundle could not be minted")

    h.changesets.create = refuse
    try:
        with pytest.raises(RuntimeError):
            h.engine.prepare(change.id)
    finally:
        h.changesets.create = original

    assert h.engine.require(change.id).stage != ch.READY
    assert _versions(h, change.id) == []


def _raise(_change):
    raise RuntimeError("the process died between the two writes")
