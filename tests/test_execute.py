"""Execution: calendar first, then email, and never a blind resend.

The external writes are not atomic and this module does not pretend they are. What it must do is
make each effect's state true — including "we do not know" — and stop rather than push a second
effect on top of a first one that did not land.
"""
from __future__ import annotations

import json

import httpx
import pytest

from privatemesh.changeset import StaleChangeSet
from privatemesh.execute import (DONE, FAILED, PENDING, RUNNING, SUMMARY_BOTH, SUMMARY_NONE,
                                SUMMARY_PARTIAL, UNKNOWN, NotApproved)
from privatemesh.ports import EtagMismatch

from fakes_v2 import Harness, at


@pytest.fixture
def h():
    return Harness()


def test_the_happy_path_updates_the_invite_then_sends_the_reply(h):
    outcome = h.executor.execute(h.approved_changeset().id)
    assert [e.state for e in outcome.effects] == [DONE, DONE]
    assert outcome.summary == "Invite updated. Reply sent."
    surfaces = [e["surface"] for e in h.rec.entries]
    assert surfaces.index("calendar") < surfaces.index("mail"), "the reply went out first"
    assert h.rec.surfaces("calendar")[0]["payload"]["if_match"] == "etag-1"
    assert h.calendar.get_event("ev1").start == at("15:00")
    assert len(h.mail.sent) == 1


def test_the_calendar_step_is_confirmed_by_reading_the_event_back(h):
    h.calendar.ignores_update = True          # the provider answered 200 and changed nothing
    outcome = h.executor.execute(h.approved_changeset().id)
    assert outcome.effects[0].state == UNKNOWN
    assert "read back" in outcome.effects[0].detail
    assert outcome.effects[1].state == PENDING and not h.mail.sent


def test_an_etag_mismatch_mid_execution_stops_and_sends_nothing(h):
    h.calendar.fail_update = EtagMismatch("412 from the provider")
    outcome = h.executor.execute(h.approved_changeset().id)
    assert outcome.effects[0].state == FAILED
    assert outcome.effects[1].state == PENDING
    assert outcome.summary == "Invite not updated. Reply not sent."
    assert not h.mail.sent


def test_an_event_that_moved_before_execution_is_refused_at_admission(h):
    cs = h.approved_changeset()
    h.calendar.update_event("ev1", "etag-1", start=at("11:00"), end=at("11:45"),
                            location="elsewhere", send_updates="none")
    with pytest.raises(StaleChangeSet):
        h.executor.execute(cs.id)
    assert not h.mail.sent
    assert h.approvals.get(cs.calendar.approval_id).status == "approved", \
        "the approval was spent on an execution that never started"


def test_a_failed_reply_reports_the_partial_state_it_actually_reached(h):
    h.mail.fail_send = RuntimeError("smtp said no")
    outcome = h.executor.execute(h.approved_changeset().id)
    assert [e.state for e in outcome.effects] == [DONE, FAILED]
    assert outcome.summary == "Invite updated. Reply not sent."


def test_an_ambiguous_send_is_reconciled_and_never_resent(h):
    """The provider took the message and the answer was lost. Resending would double it."""
    h.mail.fail_send = httpx.ReadTimeout("timed out")
    h.mail.ghost_send = True
    outcome = h.executor.execute(h.approved_changeset().id)
    assert [e.state for e in outcome.effects] == [DONE, DONE]
    assert "reconcil" in outcome.effects[1].detail
    assert len(h.mail.sent) == 1
    assert h.approvals.get(h.changesets.latest("c1").email.approval_id).status == "consumed"


def test_an_ambiguous_send_that_never_landed_is_failed_and_not_retried(h):
    h.mail.fail_send = httpx.ReadTimeout("timed out")
    outcome = h.executor.execute(h.approved_changeset().id)
    assert outcome.effects[1].state == FAILED
    assert "not resent" in outcome.effects[1].detail
    assert h.mail.sent == []
    assert len(h.rec.surfaces("mail")) == 1, "the reply was attempted twice"


def test_an_unapproved_bundle_cannot_execute(h):
    cs = h.pending_changeset()
    with pytest.raises(NotApproved):
        h.executor.execute(cs.id)
    assert not h.mail.sent and h.rec.surfaces("calendar") == []


def test_execution_is_not_repeatable_on_the_same_approvals(h):
    """Single-use: a second call finds the approvals spent and refuses to move the invite again."""
    cs = h.approved_changeset()
    h.executor.execute(cs.id)
    before = len(h.rec.surfaces("calendar"))
    with pytest.raises(NotApproved):
        h.executor.execute(cs.id)
    assert len(h.rec.surfaces("calendar")) == before
    assert len(h.mail.sent) == 1


def test_every_effect_transition_is_receipted(h):
    h.executor.execute(h.approved_changeset().id)
    # The audit stores `detail` as canonical JSON text (v1's contract), so a reader parses it.
    states = [json.loads(r["detail"])["state"] for r in h.audit.rows()
              if r["event"] == "effect.state"]
    assert states == [RUNNING, DONE, RUNNING, DONE]

    assert h.audit.verify().ok


# ------------------------------------------------- the heading is true of both effects ---
def test_the_outcome_heading_is_true_of_each_effect_separately():
    """The sixth review's Medium, at the level of the rule.

    `_summary` asked one question — "is the email `done`?" — and every other state fell through to
    "Reply not sent." For `unknown` that is not a coarse answer, it is a false one: the state is set
    when the send timed out AFTER the request left, so the message may already have been delivered
    while the page's largest sentence says it was not. `web/app/page.tsx` renders this string as the
    heading over a receipt that already says delivery is unknown, and the two contradicting each
    other on one screen is the finding.

    Every state is driven, for both effects, so a state that is added later has to be given a
    sentence rather than inheriting one that reads like a fact."""
    from privatemesh.execute import Effect, _summary

    def heading(calendar_state, email_state):
        return _summary(Effect("calendar_update", calendar_state),
                        Effect("email_reply", email_state))

    # The three sentences the product already used are unchanged, which is what keeps this a fix to
    # the unknown case rather than a rewrite of every outcome the owner has ever read.
    assert heading(DONE, DONE) == SUMMARY_BOTH
    assert heading(DONE, FAILED) == SUMMARY_PARTIAL
    assert heading(FAILED, PENDING) == SUMMARY_NONE

    for state in (PENDING, RUNNING, DONE, FAILED, UNKNOWN):
        for calendar_state in (PENDING, RUNNING, DONE, FAILED, UNKNOWN):
            line = heading(calendar_state, state)
            assert line.strip() and line.count(".") == 2, line
            assert "  " not in line, f"a gap where a sentence should be: {line!r}"
            if state in (UNKNOWN, RUNNING):
                assert "Reply not sent." not in line, (
                    f"an email in state {state!r} was reported as not sent: {line!r}")
            if calendar_state in (UNKNOWN, RUNNING):
                assert "Invite not updated." not in line, (
                    f"an invite in state {calendar_state!r} was reported as not updated: {line!r}")

    assert heading(DONE, UNKNOWN) == "Invite updated. Whether the reply was sent is unknown."


def test_an_unconfirmed_calendar_step_never_reads_as_a_failed_one(h):
    """The same rule where the owner meets it: a real run whose calendar step could not be confirmed.

    The provider answered 200 and changed nothing, so the effect is `unknown` and the reply was never
    attempted. The heading has to carry that, because the sample's own `unknown` variant already
    words it correctly and the live path did not — one screen, two answers."""
    h.calendar.ignores_update = True
    outcome = h.executor.execute(h.approved_changeset().id)
    assert [e.state for e in outcome.effects] == [UNKNOWN, PENDING]
    assert outcome.summary == ("Whether the invite was updated is unknown. Reply not sent."), \
        outcome.summary
