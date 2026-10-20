"""Round 8: the doors round 7's table said were closed, and one it never listed.

Round 7 established the rule — every outbound field is engine-rendered with provenance, derived from
the approved plan, or computed at send time with a stated reason — and then published a table
claiming every field on the wire was one of the three. The eighth review found two doors where that
was not true, one of them demonstrated end to end, and two more where the table had been generous:

  FINDING 1  the DIGEST PLAN itself. `compose_digest`'s provenance protected the normal path and
             nothing else: `Executor.send_digest` took a caller-built `DigestPlan` and sent it, and
             a direct probe put `OWNER NEVER APPROVED THIS` in a message to the care circle. This is
             not a field that leaked — it is the object the fields come from, which is why no check
             on the plan's CONTENT could ever have refused it.
  FINDING 2  the VERSION the fields were bound to. Every port bound its fields once and then called
             the provider, and `edit` mints version n+1 without invalidating an approval that has
             already been spent — so a revision landing in that window left v1's bytes leaving under
             an approval the owner had replaced.
  FINDING 3  the recipient DISPLAY NAME. The gate compared bare addresses, which is right for its
             question and is not what the adapter serialises: `Finance <alex@example.com>` reached
             the `To` header under an approval raised over `alex@example.com`.
  FINDING 4  `start.timeZone`/`end.timeZone`, added by the calendar adapter from the event it read
             at the moment of the call — the one field that decides how the provider READS the
             approved instant, and the one field no approval had ever named.

Every test names the line whose removal turns it red — see `redteam/mutcheck_findings.py
--care-r8` — so none of them is a test that can only pass.
"""
from __future__ import annotations

import pytest

from privatemesh import care as care_rules
from privatemesh.boundary import Fragment
from privatemesh.care import (DigestLine, DigestPlan, DigestPlanForged, compose_digest, digest_email,
                              plan_is_port_sealed, plan_is_sealed)
from privatemesh.execute import CALENDAR_BOUND_FIELDS, EMAIL_BOUND_FIELDS
from privatemesh.gcal import CalendarError, GCalClient
from privatemesh.boundary import ApprovedArgumentsRefused
from privatemesh.ports import CalendarEvent
from privatemesh.sim import SAMPLE_DATE

from fakes_v2 import Harness, at, invite

FORGED_TEXT = "OWNER NEVER APPROVED THIS"


def _two_meetings():
    return [invite(), invite("evt-standup", start="09:15", end="09:30", etag="etag-standup")]


@pytest.fixture
def h():
    return Harness(events=_two_meetings())


def _effect(outcome, kind):
    return next(e for e in outcome.effects if e.kind == kind)


def _forge(records=None, day=SAMPLE_DATE):
    """A plan that satisfies every check the composer makes and that nobody composed.

    Built by HAND rather than by calling `compose_digest` with a doctored record set, because the
    two are different doors and this is the demonstrated one: the probe the reviewer ran constructed
    a `DigestPlan` directly and handed it to `digest_email`.
    """
    return DigestPlan(day=day, recipients=("alex@example.com",),
                      fragments=(Fragment("care_coming", FORGED_TEXT),))


# ============================================================= finding 1 — the plan --------------
def test_finding_01_a_hand_built_plan_is_refused_by_the_composer_s_door():
    """The reviewer's probe, at the function it reached.

    A `DigestPlan` is a frozen dataclass, so `digest_email` could not tell one the engine built from
    one a caller assembled — and the probe put `OWNER NEVER APPROVED THIS` in an outbound body that
    way. Red first: with `plan_is_sealed` removed from `digest_email`, the forged body renders.
    """
    with pytest.raises(DigestPlanForged, match="did not come out of the engine's composer"):
        digest_email(_forge(), gate=None, snapshot=object())


def test_finding_01_a_hand_built_plan_is_refused_at_the_executor_port(h):
    """The same plan at the door that SENDS, which is the one the review named.

    `send_digest` was the production path: it took the plan, claimed the day, and handed it to
    `digest_email` — so a caller-built plan reached the provider through the product's own route.
    Red first: with the port's seal check removed, `h.mail.sent[-1]` carries the forged sentence.
    """
    from privatemesh.care import CareCircleStore, DigestLedger
    h.executor.care_circle = _circle()
    h.executor.digests = DigestLedger()
    with pytest.raises(DigestPlanForged, match="was not built by this engine"):
        h.executor.send_digest(_forge())
    assert not h.mail.sent, "a plan no composer built reached the care circle"


def test_finding_01_composing_from_a_caller_record_set_is_not_enough(h):
    """The door one line over, and the reason the port seals with a key of its own.

    `compose_digest` is a public function and its `records` argument is the caller's. A caller who
    composes a plan from a record set of its own making satisfies every check that function makes —
    the line names a record, the record exists, the text IS that record's render — because the
    question those checks answer is "does this citation hold", not "who wrote the record". The
    port's own seal is what makes the second question answerable, and this is the test that says
    the composer's seal alone would not have been enough.
    """
    from privatemesh.care import CareCircleStore, DigestLedger
    h.executor.care_circle = _circle()
    h.executor.digests = DigestLedger()
    plan = compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",),
                          records={"x": FORGED_TEXT},
                          coming=[DigestLine("care_coming", "x", FORGED_TEXT)])
    assert plan_is_sealed(plan), "the control: the composer did seal this plan"
    with pytest.raises(DigestPlanForged):
        h.executor.send_digest(plan)
    assert not h.mail.sent


def test_finding_01_a_plan_sealed_by_another_executor_is_not_this_executor_s(h):
    """Per-instance keys, so a seal is a statement about WHICH engine built the plan.

    A key shared by every executor in the process would make the port's check answer "some executor
    built this", and the second executor in a test, a second tenant, or anything holding a
    reference to it could send the first one's plans.
    """
    from privatemesh.care import DigestLedger
    h.executor.care_circle = _circle()
    h.executor.digests = DigestLedger()
    other = Harness(events=_two_meetings())
    other.executor.care_circle = _circle()
    other.executor.digests = DigestLedger()
    mine = h.executor.build_digest(SAMPLE_DATE, now="2026-09-20T09:00:00+01:00")
    assert plan_is_port_sealed(mine, key=h.executor._digest_key)
    assert not plan_is_port_sealed(mine, key=other.executor._digest_key)
    with pytest.raises(DigestPlanForged):
        other.executor.send_digest(mine)


def test_finding_01_editing_a_sealed_plan_breaks_the_seal(h):
    """The seal is over the plan's OWN artifact, so it is not a stamp on the envelope.

    Editing a word after composition has to take the proof with it — otherwise the seal would say
    "a composer built something like this", which is the claim that let the first door open.
    """
    import dataclasses
    from privatemesh.care import DigestLedger
    h.executor.care_circle = _circle()
    h.executor.digests = DigestLedger()
    mine = h.executor.build_digest(SAMPLE_DATE, now="2026-09-20T09:00:00+01:00")
    edited = dataclasses.replace(mine, fragments=(Fragment("care_coming", FORGED_TEXT),))
    assert not plan_is_port_sealed(edited, key=h.executor._digest_key)
    assert not plan_is_sealed(edited)


def test_finding_01_control_the_engine_s_own_plan_still_sends(h):
    """The control leg. A refusal is only evidence if the same call with the engine's own plan goes:
    without this the rows above would be rows about a port that refuses every digest."""
    from privatemesh.care import DigestLedger
    h.executor.care_circle = _circle()
    h.executor.digests = DigestLedger()
    plan = h.executor.build_digest(SAMPLE_DATE, now="2026-09-20T09:00:00+01:00")
    sent = h.executor.send_digest(plan)
    assert sent["day"] == plan.day
    assert h.mail.sent, "the engine's own digest did not go out"
    assert FORGED_TEXT not in h.mail.sent[-1]["body"]


def test_finding_01_both_bound_field_lists_are_the_closed_ones():
    """One list each, so the port, the door-probe table and this file cannot disagree about what
    "bound" covers — the eighth round's two new fields included."""
    assert CALENDAR_BOUND_FIELDS == ("event_id", "if_match_etag", "start", "end", "location",
                                     "send_updates", "timezone")
    assert EMAIL_BOUND_FIELDS == ("to", "in_reply_to", "idempotency_key")


def _circle():
    from privatemesh.care import CareCircleStore
    circle = CareCircleStore()
    circle.add(name="Alex", email="alex@example.com", may_receive=["digest"], by="owner")
    return circle


# ============================================================= finding 2 — the version -----------
class _StoreThatRevises:
    """The change-set store, wrapped so the Nth read about a bundle answers after a revision.

    This is the interleave and nothing else. A port binds its fields from one read and asks again at
    the call; the revision lands between the two, which is the window the eighth review named —
    `edit` mints version n+1 without invalidating an approval that has already been spent, so
    nothing else in the engine refuses it. `reads` is exposed so the test FAILS LOUDLY if the port's
    read pattern changes, rather than quietly revising at the wrong moment and passing.
    """

    def __init__(self, store, *, revise_on: int, revise):
        self._store, self._revise_on, self._revise, self.reads = store, revise_on, revise, 0

    def for_change(self, change_id):
        self.reads += 1
        if self.reads == self._revise_on:
            self._revise()
        return self._store.for_change(change_id)


def _approved(h, *, calendar_over=None, email_over=None, events=None):
    """An approved bundle, built exactly as the r7/r6 fixtures build one."""
    from privatemesh.changes import _calendar_display
    from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL

    cal_args = h.calendar_args(**(calendar_over or {}))
    mail_args = h.email_args(**(email_over or {}))
    seen = h.calendar.get_event(cal_args["event_id"])
    cal = h.approvals.request("task1", CALENDAR_SKILL, cal_args)
    mail = h.approvals.request("task1", EMAIL_SKILL, mail_args)
    cs = h.changesets.create(change_id="c1", task_id="task1", event_etag=seen.etag,
                             boundary_id="pc1", boundary_version=h.boundary.version_of("pc1"),
                             calendar_args=cal_args, email_args=mail_args,
                             calendar_display=_calendar_display(seen, cal_args),
                             approval_ids=(cal.id, mail.id))
    cs = h.changesets.approve(cs.id, cs.version, current_event_etag=seen.etag,
                              current_boundary_version=h.boundary.version_of("pc1"))
    assert cs.status == "approved", "the fixture failed to approve, so nothing was tested"
    return cs


def _revise_once(h, cs):
    """The owner edits the change while the call is in flight: version 2, minted over fresh
    approvals. The old ones are consumed, so `_invalidate` skips them by design — which is why the
    revision succeeds and why nothing but the port can notice it."""
    from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL
    cal = h.approvals.request("task1", CALENDAR_SKILL, h.calendar_args())
    mail = h.approvals.request("task1", EMAIL_SKILL, h.email_args())
    return h.changesets.edit(cs.id, approval_ids=(cal.id, mail.id))


def test_finding_02_a_revision_after_the_calendar_binding_stops_the_write(h):
    """The exact interleave, at the calendar port.

    approve v1 → the broker consumes the approval → the port binds v1's fields → the owner revises
    to v2 → the port is at the call. Read 1 is the binding and read 2 is the check at the call, so
    the revision is triggered from read 2 and the write is refused.

    Red first: with `_still_at_the_bound_version` removed from `update_event`, the meeting moves and
    `h.rec.surfaces("calendar")` is non-empty.
    """
    cs = _approved(h)
    store = _StoreThatRevises(h.changesets, revise_on=2, revise=lambda: _revise_once(h, cs))
    h.change_resolver.bundles = store

    outcome = h.executor.execute(cs.id)

    effect = _effect(outcome, "calendar_update")
    assert effect.state == "failed", "a plan the owner had already replaced moved the invite"
    assert "revised while this calendar write was in flight" in effect.detail
    assert not h.rec.surfaces("calendar")
    assert h.calendar.get_event("ev1").start == at("15:00")
    assert h.changesets.for_change("c1").version == 2, "the fixture did not actually revise"
    assert store.reads == 2, (
        f"the port made {store.reads} reads about the bundle, not 2 — the revision landed at a "
        f"different moment than this test claims, so it is not testing the window it names")


def test_finding_02_a_revision_after_the_reply_binding_stops_the_send(h):
    """The same interleave at the reply port, which is the effect nobody can recall.

    The calendar effect runs first and completes over reads 1 and 2; the reply binds on read 3 and
    checks on read 4, so the revision lands there. The invite legitimately moved — that effect was
    approved and finished — and what the check prevents is the REPLY, which is why `reads == 4` is
    asserted rather than assumed.
    """
    cs = _approved(h)
    store = _StoreThatRevises(h.changesets, revise_on=4, revise=lambda: _revise_once(h, cs))
    h.change_resolver.bundles = store

    outcome = h.executor.execute(cs.id)

    assert _effect(outcome, "calendar_update").state == "done", (
        f"the calendar leg was supposed to complete: {_effect(outcome, 'calendar_update').detail}")
    effect = _effect(outcome, "email_reply")
    assert effect.state == "failed", "a reply the owner had already replaced was sent anyway"
    assert "revised while this reply was in flight" in effect.detail
    assert not h.mail.sent, "the superseded reply left"
    assert store.reads == 4, (
        f"the port made {store.reads} reads about the bundle, not 4 — the revision landed at a "
        f"different moment than this test claims")


def test_finding_02_control_an_untouched_change_still_runs_both_effects(h):
    """The control leg, on a store that never revises: both effects complete. Without it the two
    rows above would be rows about a port that refuses every write."""
    cs = _approved(h)
    outcome = h.executor.execute(cs.id)
    assert _effect(outcome, "calendar_update").state == "done"
    assert _effect(outcome, "email_reply").state == "done"
    assert h.mail.sent


def test_finding_02_a_change_that_cannot_be_read_back_is_refused(h):
    """The fail-closed half: a resolver that cannot answer is not a resolver that said "unchanged".
    "Could not check" is the state the round-6 ports already refuse on, and this is the same rule
    asked of the version."""
    from privatemesh.execute import _still_at_the_bound_version

    class _Unreadable:
        def for_change(self, change_id):
            raise RuntimeError("the store is unavailable")

    with pytest.raises(ApprovedArgumentsRefused, match="revised while this reply was in flight"):
        _still_at_the_bound_version(_Unreadable(), "c1", "email", type("B", (), {"version": 1})(),
                                    port="reply")


# ============================================================= finding 3 — the display name -------
def test_finding_03_a_display_name_the_owner_did_not_approve_is_refused(h):
    """The Medium, at the door it reached.

    `guard_recipients` compares bare addresses — deliberately, because a display name must not
    decide who is ALLOWED — and the bare address is not what the adapter serialises: `build_reply`
    writes `", ".join(to)`. So an approved `alex@example.com` and a called
    `Finance <alex@example.com>` are the same address and two different headers, and the second is
    the one the recipient reads.

    Red first: with `to` removed from `EMAIL_BOUND_FIELDS`, `h.mail.sent[-1]["to"]` carries the
    unapproved display name.
    """
    cs = _approved(h, email_over={"to": ["Finance <alex@example.com>"]})
    # The APPROVAL is raised over the attack and the bundle holds the approved value, which is the
    # split the finding is about: `create` stores what the owner read, the broker runs what the
    # approval was raised over.
    from privatemesh.changes import _calendar_display
    from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL
    cs = h.changesets.create(change_id="c1", task_id="task1",
                             event_etag=h.calendar.get_event("ev1").etag,
                             boundary_id="pc1", boundary_version=h.boundary.version_of("pc1"),
                             calendar_args=h.calendar_args(),
                             email_args=h.email_args(),
                             calendar_display=_calendar_display(h.calendar.get_event("ev1"),
                                                                h.calendar_args()),
                             approval_ids=(h.approvals.request("t2", CALENDAR_SKILL,
                                                               h.calendar_args()).id,
                                           h.approvals.request(
                                               "t2", EMAIL_SKILL,
                                               h.email_args(to=["Finance <alex@example.com>"])).id))
    cs = h.changesets.approve(cs.id, cs.version,
                              current_event_etag=h.calendar.get_event("ev1").etag,
                              current_boundary_version=h.boundary.version_of("pc1"))

    outcome = h.executor.execute(cs.id)
    effect = _effect(outcome, "email_reply")
    assert effect.state == "failed"
    assert "to" in effect.detail, "the refusal does not name the field it refused"
    assert not h.mail.sent, "an unapproved display name reached the To header"


def test_finding_03_control_the_approved_audience_still_sends(h):
    """The control: the approved representation in the same place sends, and the wire carries
    exactly it."""
    cs = _approved(h)
    outcome = h.executor.execute(cs.id)
    assert _effect(outcome, "email_reply").state == "done", _effect(outcome, "email_reply").detail
    assert h.mail.sent[-1]["to"] == ["alex@example.com"]


# ============================================================= finding 4 — the zone ----------------
def test_finding_04_a_zone_the_owner_did_not_approve_is_refused(h):
    """The Medium from the adapter's side: `start.timeZone` decided by the call rather than bound.

    The zone tells the provider how to READ the approved instant, so a write carrying one nobody
    approved is a wall clock the owner never saw — even when every other field matches.

    Red first: with `timezone` removed from `CALENDAR_BOUND_FIELDS`, the write lands carrying
    whatever zone the call named.
    """
    from privatemesh.changes import _calendar_display
    from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL
    approved_cal, attacked_cal = h.calendar_args(), h.calendar_args(timezone="America/New_York")
    seen = h.calendar.get_event("ev1")
    mail_args = h.email_args()
    cal = h.approvals.request("task1", CALENDAR_SKILL, attacked_cal)
    mail = h.approvals.request("task1", EMAIL_SKILL, mail_args)
    cs = h.changesets.create(change_id="c1", task_id="task1", event_etag=seen.etag,
                             boundary_id="pc1", boundary_version=h.boundary.version_of("pc1"),
                             calendar_args=approved_cal, email_args=mail_args,
                             calendar_display=_calendar_display(seen, approved_cal),
                             approval_ids=(cal.id, mail.id))
    cs = h.changesets.approve(cs.id, cs.version, current_event_etag=seen.etag,
                              current_boundary_version=h.boundary.version_of("pc1"))

    outcome = h.executor.execute(cs.id)
    effect = _effect(outcome, "calendar_update")
    assert effect.state == "failed"
    assert "timezone" in effect.detail, "the refusal does not name the field it refused"
    assert not h.rec.surfaces("calendar")


def test_finding_04_control_the_approved_zone_is_what_the_port_hands_over(h):
    """The control, and it asserts the VALUE rather than the outcome: the payload the port produced
    carries the zone the engine put on the bundle. A fake that substituted the event's own zone
    would satisfy an outcome-only assertion while the port passed nothing."""
    cs = _approved(h)
    outcome = h.executor.execute(cs.id)
    assert _effect(outcome, "calendar_update").state == "done", (
        _effect(outcome, "calendar_update").detail)
    payload = h.rec.surfaces("calendar")[0]["payload"]
    assert payload["timezone"] == "Europe/London"


def test_finding_04_the_adapter_refuses_a_zone_the_provider_no_longer_serves():
    """The adapter's half, driven directly because no fake can reach it.

    The engine hands over the approved zone; the provider is asked to AGREE with it rather than
    asked what it is. A mismatch refuses instead of picking a winner — the approved zone writes a
    wall clock against an event that no longer has it, and the served zone writes an interpretation
    nobody approved.

    Red first: with the `zone != served` refusal removed, the PATCH is sent with the caller's zone.
    """
    client = GCalClient(account="probe")
    served = CalendarEvent(id="ev1", etag="etag-1", summary="Alex Doe and You",
                           start=at("15:00"), end=at("15:45"), timezone="Europe/London",
                           location="Old Cafe, King's Cross", attendees=("alex@example.com",))
    calls: list[dict] = []
    client.get_event = lambda event_id: served
    client._call = lambda method, path, **kw: (calls.append(kw), {"id": "ev1"})[1]

    with pytest.raises(CalendarError, match="the plan the owner approved was written in"):
        client.update_event("ev1", "etag-1", start=at("15:00"), end=at("15:45"),
                            location="The Standard, King's Cross", send_updates="all",
                            timezone="America/New_York")
    assert not calls, "a zone nobody approved reached the provider"

    # The control, in the same shape: the approved zone is written, and the body carries it.
    client.update_event("ev1", "etag-1", start=at("15:00"), end=at("15:45"),
                        location="The Standard, King's Cross", send_updates="all",
                        timezone="Europe/London")
    body = calls[-1]["json_body"]
    assert body["start"] == {"dateTime": at("15:00"), "timeZone": "Europe/London"}
    assert body["end"] == {"dateTime": at("15:45"), "timeZone": "Europe/London"}


def test_finding_04_an_adapter_with_no_approved_zone_still_reads_the_event_s_own():
    """The compatibility leg, stated rather than left implied: an adapter called without a zone —
    the sample path, a direct caller — falls back to the event's own zone as it always did. The
    binding is the ENGINE's obligation, and this is what "not given one" means."""
    client = GCalClient(account="probe")
    served = CalendarEvent(id="ev1", etag="etag-1", summary="Alex Doe and You",
                           start=at("15:00"), end=at("15:45"), timezone="Europe/London",
                           location="Old Cafe, King's Cross", attendees=("alex@example.com",))
    calls: list[dict] = []
    client.get_event = lambda event_id: served
    client._call = lambda method, path, **kw: (calls.append(kw), {"id": "ev1"})[1]
    client.update_event("ev1", "etag-1", start=at("15:00"), end=at("15:45"),
                        location="The Standard, King's Cross", send_updates="all")
    assert calls[-1]["json_body"]["start"]["timeZone"] == "Europe/London"
