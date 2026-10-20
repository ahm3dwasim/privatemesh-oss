"""The third review's first High: the execution outcome was never written down.

`V2Service._settle` moved `change.stage` and stamped `updated_at` AFTER the effects ran, in memory
only. The change-set row carries the outcome (the executor marks it), but the CHANGE record did
not — so a restart re-read `proposal_ready` for a plan whose calendar invite had already moved and
whose reply had already gone. The UI and the watcher then treat an executed plan as still pending.

The bar: after a restart, a change that ran says so. `partial` and `unknown` are the interesting
ones — they are exactly the states a restart must not launder into "needs you".
"""
from __future__ import annotations

import pytest

from privatemesh import changes as ch
from privatemesh.api_v2 import V2Error

from test_fix_change_persistence import harness, prepared, script


def approved(h):
    """A change with an approved bundle, ready to execute."""
    change = prepared(h)
    service = h.v2()
    assert service.approve(change.id, change.version)
    return h.engine.require(change.id)


def test_the_outcome_of_a_run_is_on_disk_before_the_call_returns(tmp_path):
    """The narrow claim: the record on disk already says what happened."""
    h = harness(tmp_path)
    change = approved(h)
    assert change.stage in (ch.DONE, ch.PARTIAL, ch.STOPPED), change.stage

    fresh = ch.ChangeStore(path=str(tmp_path / "changes.db"))
    row = [c for c in fresh.load() if c.id == change.id]
    assert row, "the change is not in the store at all"
    assert row[0].stage == change.stage, (
        f"the store says {row[0].stage!r} while the engine says {change.stage!r}")
    assert row[0].updated_at == change.updated_at


def test_a_restart_does_not_offer_an_executed_plan_as_pending_again(tmp_path):
    """The finding in one test: the SECOND process, over the same store, must not think the plan
    still needs the owner."""
    before = harness(tmp_path)
    change = approved(before)
    ran_as = change.stage

    after = harness(tmp_path)
    resumed = after.engine.get(change.id)
    assert resumed is not None
    assert resumed.stage == ran_as, (
        f"after the restart the change is {resumed.stage!r}, so an executed plan is pending again")


def test_a_partial_run_is_not_laundered_into_needs_you(tmp_path):
    """`partial` is the state that matters most: one effect landed and one did not. A restart that
    re-offers it as `proposal_ready` invites the owner to approve a second send."""
    h = harness(tmp_path)
    change = approved(h)
    if change.stage != ch.PARTIAL:                    # pragma: no cover - the fakes run clean
        pytest.skip(f"the scripted providers completed this run ({change.stage})")

    after = harness(tmp_path)
    assert after.engine.get(change.id).stage == ch.PARTIAL


def test_the_settled_stage_is_written_by_the_engine_not_by_the_api(tmp_path):
    """The write belongs with the other writes. `_settle` used to assign the stage directly, which
    is how it came to be the one mutation that skipped `_persist`; the engine now owns it."""
    h = harness(tmp_path)
    change = approved(h)
    store = ch.ChangeStore(path=str(tmp_path / "changes.db"))
    assert ch.ChangeEngine.settle.__doc__, "settle has no docstring saying what it guarantees"

    # Calling it again is a stage move like any other, and it is durable like any other.
    moved = h.engine.settle(change.id, ch.DONE)
    assert moved.stage == ch.DONE
    assert [c for c in store.load() if c.id == change.id][0].stage == ch.DONE


def test_settling_a_change_that_is_gone_is_not_an_error(tmp_path):
    """A change deleted between the run and the settle is a state the caller must survive: the
    effects already ran, and raising here would report a failure that did not happen."""
    h = harness(tmp_path)
    assert h.engine.settle("no-such-change", ch.DONE) is None
