"""The live run's fourth defect: every proposal lived and died with the process.

`ChangeEngine._changes` is a dict. A restart lost every change the owner was looking at, while
`routine_seen` still marked the email that produced it as handled — so the plan was not merely
lost, it was unreachable: the mailbox would never offer that email again.

The change records now live in the same place every other live store does, one file under the
engine's data dir. The bar this file holds them to is not "a row exists": it is that a restart
hands back a `proposal_ready` change whose change set is still the one it was prepared against, so
the owner can approve exactly what they were shown.
"""
from __future__ import annotations

import pytest

from privatemesh import changes as ch
from privatemesh.changeset import StaleChangeSet
from privatemesh.wiring import _db

from fakes_v2 import Harness, at, change_email, evidence

TWO_CAFES = {"king's cross": [
    evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
             "Open daily 12:00-23:00", official=True),
    evidence("https://cafezwei.example/", "Cafe Zwei", "Open daily 08:00-18:00", official=True)]}


def script(**over):
    base = {
        "classify": {"is_meeting_change": True},
        "extract": {"options": [{"start": at("15:00"), "end": at("15:45"), "phrase": "3"}],
                    "request_excerpt": "Could we meet near King's Cross?"},
        "query": {"query": "cafes near King's Cross opening hours", "include_domains": []},
        "choose": {"venue_index": 0, "is_venue": True,
                   "reason_sentence": "Its own page gives the hours.",
                   "rejected": [{"index": 1, "reason": "quieter"}]},
        "draft": {"template_body": "Hi Alex,",
                  "fragments": [{"source": "time", "text": "15:00-15:45 works for me."},
                                {"source": "venue", "text": "The Standard, King's Cross."}]},
    }
    base.update(over)
    return base


def harness(data_dir, **kw):
    kw.setdefault("messages", [change_email("m1")])
    return Harness(script=script(**kw.pop("script", {})), search=TWO_CAFES,
                   data_dir=data_dir, **kw)


def prepared(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    return change


# ---------------------------------------------------------------- the restart -----------------
def test_a_restart_resumes_a_proposal_ready_change(tmp_path):
    """The defect in one test: the same change, in a second process, still planable."""
    before = harness(tmp_path)
    change = prepared(before)

    after = harness(tmp_path)                       # a NEW engine over the SAME store
    resumed = after.engine.get(change.id)
    assert resumed is not None, "the change did not survive the restart"
    assert resumed.stage == ch.READY
    assert resumed.change_set_id == change.change_set_id
    assert resumed.version == change.version


def test_the_resumed_change_is_still_bound_to_the_change_set_it_was_prepared_against(tmp_path):
    """The version has to MATCH, not merely exist. An approval raised over a plan the engine has
    since re-minted is exactly the stale bundle `ChangeSetStore.approve` refuses."""
    before = harness(tmp_path)
    change = prepared(before)
    cs_version = before.changesets.for_change(change.id).version

    after = harness(tmp_path)
    cs = after.changesets.for_change(change.id)
    assert cs is not None
    assert cs.version == cs_version
    assert cs.change_id == change.id
    assert cs.status == "pending"
    # And the bundle the owner is shown is still spendable, at the etag and boundary they reviewed.
    approved = after.changesets.approve(
        cs.id, cs.version, current_event_etag=after.calendar.get_event("ev1").etag,
        current_boundary_version=after.boundary.version_of("pc1"))
    assert approved.status == "approved"


def test_the_whole_plan_comes_back_not_just_the_identifier(tmp_path):
    """A resumed change that has lost its venue or its slots is not the plan the owner reviewed."""
    before = harness(tmp_path)
    change = prepared(before)

    resumed = harness(tmp_path).engine.get(change.id)
    assert resumed.proposal == change.proposal
    assert resumed.request_excerpt == change.request_excerpt
    assert resumed.subject == change.subject
    assert resumed.from_address == change.from_address
    assert resumed.thread_id == change.thread_id
    assert resumed.message_id == change.message_id
    assert resumed.updated_at == change.updated_at


def test_a_restart_does_not_re_propose_an_email_that_was_already_handled(tmp_path):
    """The live failure was silence: `routine_seen` said handled, and the plan was gone.

    The change is reachable after the restart without the mailbox being read again — which is the
    only way the owner ever sees it, because the watcher will not offer that email twice."""
    before = harness(tmp_path)
    change = prepared(before)

    after = harness(tmp_path)
    listed = {c.id: c for c in after.engine.changes()}
    assert change.id in listed
    assert listed[change.id].proposal is not None
    assert listed[change.id].plan_state == "needs_you"


def test_nothing_is_re_read_from_the_mailbox_to_resume(tmp_path):
    """Resuming is a store read, not a second classify: the watcher must not spend a model call
    reconstructing what it already recorded."""
    before = harness(tmp_path)
    prepared(before)

    after = harness(tmp_path)
    assert after.mail.polls == 0
    assert after.tf.calls == []


def test_the_change_survives_a_restart_more_than_once(tmp_path):
    before = harness(tmp_path)
    change = prepared(before)

    middle = harness(tmp_path)
    assert middle.engine.get(change.id).stage == ch.READY

    last = harness(tmp_path)
    assert last.engine.get(change.id).proposal == change.proposal


# ---------------------------------------------------------------- what else must persist ------
def test_an_edit_before_the_restart_is_the_version_that_comes_back(tmp_path):
    """Edits mint versions. The resumed change must be the LAST one, and its bundle current."""
    before = harness(tmp_path)
    change = prepared(before)
    version_before, cs_before = change.version, before.changesets.for_change(change.id).version

    revised = before.engine.revise(change.id, venue_index=1)
    assert revised.version == version_before + 1
    current = before.changesets.for_change(change.id)
    assert current.version == cs_before + 1

    after = harness(tmp_path)
    resumed = after.engine.get(change.id)
    assert resumed.version == revised.version
    assert resumed.proposal["venue"]["name"] == "Cafe Zwei"
    assert after.changesets.for_change(change.id).version == current.version


def test_a_bundle_that_truly_went_stale_is_still_refused_after_a_restart(tmp_path):
    """Persistence must not become a way around the staleness check.

    A restart that resumed a plan and waved it through would be worse than the loss it fixes."""
    before = harness(tmp_path)
    change = prepared(before)
    cs = before.changesets.for_change(change.id)

    after = harness(tmp_path)
    moved = after.calendar.update_event("ev1", after.calendar.get_event("ev1").etag,
                                        start=at("10:00"), end=at("10:45"), location="Elsewhere",
                                        send_updates="all")
    with pytest.raises(StaleChangeSet):
        after.changesets.approve(cs.id, cs.version, current_event_etag=moved.etag,
                                 current_boundary_version=after.boundary.version_of("pc1"))


def test_a_stopped_change_stays_stopped_across_a_restart(tmp_path):
    before = harness(tmp_path)
    change = prepared(before)
    before.engine.leave(change.id)

    resumed = harness(tmp_path).engine.get(change.id)
    assert resumed.stage == ch.STOPPED
    assert resumed.plan_state == "stopped"


def test_the_change_store_is_file_backed_beside_the_other_live_stores(tmp_path):
    """Same data dir, one file per store — the arrangement `wiring._db` already states."""
    h = harness(tmp_path)
    assert isinstance(h.engine.store, ch.ChangeStore)
    assert (tmp_path / "changes.db").exists()
    assert (tmp_path / "changesets.db").exists()


def test_the_sample_keeps_every_change_in_memory():
    """The judges' path restarts by design: the same change must be waiting, not already read."""
    assert _db(None, "changes.db") == ":memory:"
    h = Harness(script=script(), search=TWO_CAFES, messages=[change_email("m1")])
    change = prepared(h)
    assert isinstance(h.engine.store, ch.ChangeStore)
    assert h.engine.get(change.id) is not None
    assert _db(None, "changes.db") == ":memory:"


def test_the_engine_still_works_with_no_store_at_all():
    """`store` is optional, which is how every engine built before this lane keeps working."""
    h = Harness(script=script(), search=TWO_CAFES, messages=[change_email("m1")])
    h.engine.store = None
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == ch.READY


def test_the_day_the_plan_is_for_survives_the_restart(tmp_path):
    """`plan_day` is how the day view finds a change about tomorrow. It is engine state, so it has
    to be in the record — a resumed change with no day would vanish from the calendar screen."""
    before = harness(tmp_path)
    change = prepared(before)
    assert change.plan_day == DAY_ISO
    assert harness(tmp_path).engine.get(change.id).plan_day == DAY_ISO


DAY_ISO = "2026-09-19"
