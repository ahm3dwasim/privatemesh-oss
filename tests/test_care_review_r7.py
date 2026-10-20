"""Round 7: the fields that were never held to the rule the round-6 fix established.

Round 6 closed the BODY — "every outbound field is an engine-rendered fragment carrying the event id
it derives from, or the action is refused" — and the seventh review pointed at the same rule asked
of every OTHER field on the wire. Four of them sat outside it:

  FINDING 1  the SUBJECT. A caller argument, written straight into the provider's `Subject` header
             at both ports that have one. The body was a render of declared fragments and the
             subject was not, so the one header a recipient reads first had no provenance at all.
  FINDING 2  the ADAPTER-ADDED HEADERS. `In-Reply-To`, `References` and the idempotency header took
             caller values, and the header set itself was open: an adapter that added a `Cc` added
             it silently, because nothing enumerated what the engine had approved.
  FINDING 3  the CALENDAR ENGINE FIELDS. `start`, `end`, `event_id`, `send_updates`,
             `if_match_etag` went from the call straight into the write. The owner approves a plan;
             the port ran whatever arguments the call carried.
  FINDING 4  two PARTIAL paths the sixth round left: a missing venue catalogue or venue identity
             was reported clean instead of refused, and a digest line could name a record whose
             render it was not.

Each test names the source line whose removal turns it red — see
`redteam/mutcheck_findings.py --care-r7` — so none of them is a test that can only pass.
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest

from privatemesh import care as care_rules
from privatemesh.boundary import (ApprovedArgumentsRefused, Fragment, SUBJECT_TEMPLATES,
                                  SchemaLeak, compose_email)
from privatemesh.care import (DIGEST_SUBJECT_TEMPLATE, DigestBadLine, DigestLine, artifact_id,
                              compose_digest, digest_subject)
from privatemesh.changes import VenueUnknown, render_venue
from privatemesh.changeset import APPROVED_STATUS
from privatemesh.execute import (CALENDAR_BOUND_FIELDS, EMAIL_BOUND_FIELDS, _venue_not_rendered,
                                 CALENDAR_SKILL, EMAIL_SKILL)
from privatemesh.gmail import (APPROVED_HEADERS, GmailError, assert_approved_headers, build_reply,
                               reply_header_names)
from privatemesh.sim import SAMPLE_DATE

from fakes_v2 import Harness, at, invite
from test_redteam_guards import _approved

FOREIGN = "Your Team standup at 09:15 moves too."


def _two_meetings():
    return [invite(), invite("evt-standup", start="09:15", end="09:30", etag="etag-standup")]


@pytest.fixture
def h():
    """TWO meetings, not one. `event_id` moves to the day's other meeting, and an attack that
    names a meeting the calendar does not have is refused by the day check rather than by the field
    binding — a refusal that is not about the field the row names."""
    return Harness(events=_two_meetings())


def _effect(outcome, kind):
    return next(e for e in outcome.effects if e.kind == kind)


# --------------------------------------------------------------- the split the findings are about -
def _split_email(h, **over):
    """Approve a bundle over the engine's own arguments, raise the APPROVAL over different ones.

    This split IS the finding. `ChangeSetStore.create` stores the arguments the owner reads on the
    review screen; the broker runs `req.arguments` — the ones the approval was raised over — and
    until this round nothing compared the two. `_prepare` raises both over one dict, so a fixture
    that did the same would be asking whether a value equals itself.
    """
    from privatemesh.changes import _calendar_display

    approved, attacked = h.email_args(), h.email_args(**over)
    cal_args = h.calendar_args()
    cal = h.approvals.request("task1", CALENDAR_SKILL, cal_args)
    mail = h.approvals.request("task1", EMAIL_SKILL, attacked)
    cs = h.changesets.create(change_id="c1", task_id="task1",
                             event_etag=h.calendar.get_event("ev1").etag,
                             boundary_id="pc1", boundary_version=h.boundary.version_of("pc1"),
                             calendar_args=cal_args, email_args=approved,
                             calendar_display=_calendar_display(h.calendar.get_event("ev1"),
                                                                cal_args),
                             approval_ids=(cal.id, mail.id))
    cs = h.changesets.approve(cs.id, cs.version,
                              current_event_etag=h.calendar.get_event("ev1").etag,
                              current_boundary_version=h.boundary.version_of("pc1"))
    assert cs.status == APPROVED_STATUS, "the fixture failed to approve, so nothing was tested"
    return h.executor.execute(cs.id)


def _split_calendar(h, **over):
    """The same split on the calendar port. The etag follows the APPROVED event id, so the run gets
    past `_admit`'s precondition and the field binding is the thing that has to refuse."""
    from privatemesh.changes import _calendar_display

    approved, attacked = h.calendar_args(), h.calendar_args(**over)
    seen = h.calendar.get_event(approved["event_id"])
    mail_args = h.email_args()
    cal = h.approvals.request("task1", CALENDAR_SKILL, attacked)
    mail = h.approvals.request("task1", EMAIL_SKILL, mail_args)
    cs = h.changesets.create(change_id="c1", task_id="task1", event_etag=seen.etag,
                             boundary_id="pc1", boundary_version=h.boundary.version_of("pc1"),
                             calendar_args=approved, email_args=mail_args,
                             calendar_display=_calendar_display(seen, approved),
                             approval_ids=(cal.id, mail.id))
    cs = h.changesets.approve(cs.id, cs.version, current_event_etag=seen.etag,
                              current_boundary_version=h.boundary.version_of("pc1"))
    assert cs.status == APPROVED_STATUS, "the fixture failed to approve, so nothing was tested"
    return h.executor.execute(cs.id)


# ============================================================= finding 1 — the subject ------------
def test_finding_01_a_subject_no_source_declared_is_refused_at_the_port(h):
    """The subject is held to the same rule as the body: it must BE the render of its pieces.

    The pieces come off the change — `subject_parts` — so the string on the wire is the engine's
    `Re:` plus the thread's own words, and a subject handed to the port from anywhere else is
    refused rather than ignored. Ignoring would be the wrong fix: a check that only runs when the
    caller cooperates is not a check.

    The control is the engine's own subject in the same place, which sends — so this is a refusal of
    a value and not a port that refuses subjects.
    """
    outcome = _split_email(h, subject=FOREIGN)
    effect = _effect(outcome, "email_reply")
    assert effect.state == "failed"
    assert "subject is not the render" in effect.detail
    assert not h.mail.sent, "a subject no source declared reached the provider"

    # The control, on a fresh harness: the engine's own subject in the same place sends. A refusal
    # is only evidence if the same call with the approved value goes.
    control_h = Harness(events=_two_meetings())
    outcome = _split_email(control_h)
    assert _effect(outcome, "email_reply").state == "done", _effect(outcome, "email_reply").detail
    assert control_h.mail.sent[-1]["subject"] == "Re: Our usual cafe has closed"


def test_finding_01_a_subject_overwritten_after_composition_is_refused_at_the_port(h):
    """`guard_email` compares the finished subject against its own declared render.

    This is the half that does not care who built the message: whatever the composer did, the string
    on the object must equal `render_subject(template, fragments)` or the port refuses. Without the
    comparison the only thing between a novel subject and the wire is the protected-value scan,
    which cannot see a subject that is merely untrue.
    """
    email = compose_email(to=("alex@example.com",), subject_template="Re:",
                          subject_fragments=(Fragment(source="thread_subject",
                                                      text="Our usual cafe has closed"),),
                          fragments=(Fragment(source="template", text="Hi Alex,"),),
                          gate=h.gate, change_id="c1")
    assert email.subject == "Re: Our usual cafe has closed"      # the control
    # `OutboundEmail` is frozen, so the overwrite is a `replace` — the same state an adapter would
    # produce by rebuilding the message with a different header, which is what this leg is about.
    forged = dataclasses.replace(email, subject=FOREIGN)
    with pytest.raises(SchemaLeak, match="subject is not the render of its own declared pieces"):
        h.gate.guard_email(forged, change_id="c1", permitted=("alex@example.com",), stage="send")


def test_finding_01_a_subject_template_outside_the_closed_set_is_refused(h):
    """`SUBJECT_TEMPLATES` is the closed set of fixed prefixes, and a second entry is not addable
    from a call site: the template is checked at composition AND again at the port."""
    with pytest.raises(SchemaLeak, match="is not one of the subject templates"):
        compose_email(to=("alex@example.com",), subject_template="Fwd:",
                      subject_fragments=(Fragment(source="thread_subject", text="x"),),
                      fragments=(Fragment(source="template", text="Hi Alex,"),),
                      gate=h.gate, change_id="c1")
    assert SUBJECT_TEMPLATES == frozenset({"Re:", "Today —"}), (
        "the closed set grew; every addition to it is a new subject prefix on the wire")


def test_finding_01_a_digest_subject_is_rendered_from_the_plan_not_handed_over(h):
    """The digest port had the same hole one port over and the fix is the same shape: the subject is
    the render of the one fixed prefix and the plan's own day. There is no parameter for one."""
    plan = compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",),
                          happened=[DigestLine(source="care_happened", record_id="ev1",
                                               text="Standup at 09:15")],
                          records={"ev1": "Standup at 09:15"})
    assert digest_subject(plan) == f"{DIGEST_SUBJECT_TEMPLATE} {SAMPLE_DATE}"
    assert FOREIGN not in digest_subject(plan)
    assert "subject" not in inspect.signature(care_rules.digest_email).parameters, (
        "a subject parameter came back; a hand-typed subject is the finding this closed")


# ============================================================= finding 2 — the envelope ----------
def test_finding_02_in_reply_to_is_bound_to_the_approved_message_id(h):
    """`rfc822_message_id` decides `In-Reply-To` and `References`, so it is an outbound field — and
    the port compares it against the approved bundle before the build."""
    # The attack is an APPROVED id the provider does not serve for the pinned message: the reply
    # would be filed into a conversation nobody chose, by headers that looked ratified. This is the
    # direction that matters, because the string on the wire is read from the provider's own copy
    # and never from the call — so a caller-supplied id is not a value that can reach the header at
    # all, and the binding is what makes the approved and served values the same record.
    cs = _approved(h, rfc822_message_id="<somewhere-else@example.com>")
    assert cs.status == APPROVED_STATUS
    effect = _effect(h.executor.execute(cs.id), "email_reply")
    assert effect.state == "failed"
    assert "message id this reply threads on" in effect.detail
    assert not h.mail.sent, "the reply went out with an envelope nobody approved"

    # The control, on a fresh harness: the id the provider actually serves goes, and the wire
    # carries the PROVIDER's copy rather than the argument — which is why the caller cannot choose it.
    ok = Harness(events=_two_meetings())
    cs = _approved(ok)
    assert _effect(ok.executor.execute(cs.id), "email_reply").state == "done"
    assert ok.mail.sent[-1]["in_reply_to"] == "m1"


def test_finding_02_the_idempotency_header_is_bound_to_the_approved_key(h):
    """`X-PrivateMesh-Key` decides whether the provider sends once or twice, so a caller-supplied
    one is a caller deciding how many copies the recipient gets."""
    outcome = _split_email(h, idempotency_key="idem-attacker-v9")
    effect = _effect(outcome, "email_reply")
    assert effect.state == "failed"
    assert "idempotency_key" in effect.detail
    assert not h.mail.sent
    # `to` joined this list in round 8, and the equality is why: the audience the adapter serialises
    # is a bound field too, so a list covering only the envelope keeps reporting "bound" for a
    # header nobody compared.
    assert EMAIL_BOUND_FIELDS == ("to", "in_reply_to", "idempotency_key")


def test_finding_02_a_reply_whose_approved_plan_cannot_be_read_is_refused(h):
    """The same rule at the reply port, in the state `ApprovedBundles` actually starts in.

    The resolver is built before the store that answers it — the registry needs the skills, the
    skills need the resolver — so "not attached yet" is a state the build is genuinely in, and it
    must read as a refusal rather than as permission. The port is driven directly here because the
    executor stops at the first failed effect, and a calendar refusal would mean the reply was
    never attempted: this asks the reply's own question rather than the calendar's.
    """
    from privatemesh import skills
    from privatemesh.execute import ApprovedBundles, build_effect_skills

    tool = next(t for t in build_effect_skills(h.calendar, h.mail, gate=h.gate,
                                               changes=h.change_resolver,
                                               bundles=ApprovedBundles())
                if "email" in t.name)
    # Stated as an assertion rather than left to `pytest.raises`, because the mutant of this line
    # does not only remove the refusal: with nothing to bind to, the NEXT line indexes `None`, and
    # the test then goes red on an `AttributeError` escaping the call — red without an assertion
    # about what went wrong, which is evidence the guard is reached and none at all about the
    # sentence this test claims. See `redteam/mutcheck_findings.py`, which counts only the first.
    token = skills._ACTIVE.set(skills._BROKER_TOKEN)
    try:
        try:
            tool.fn(h.email_args())
        except ApprovedArgumentsRefused as exc:
            assert "cannot be matched against the plan" in str(exc), exc
        except Exception as exc:                                     # noqa: BLE001
            pytest.fail(f"a reply whose plan cannot be read was refused with "
                        f"{type(exc).__name__} rather than an ApprovedArgumentsRefused: {exc}")
        else:
            pytest.fail("a reply whose approved plan cannot be read was attempted anyway")
    finally:
        skills._ACTIVE.reset(token)
    assert not h.mail.sent


def test_finding_02_the_header_set_is_closed_and_an_extra_header_is_named(h):
    """`Cc`/`Bcc`/`Reply-To` were impossible by ABSENCE. This makes them impossible by
    CONSTRUCTION: the names the send path may put on the wire are read off the one builder, and any
    name outside that set is refused with the name in the message."""
    parent = h.mail.get_thread("t1")[0]
    message = build_reply(in_reply_to=parent, to=("alex@example.com",), subject="Re: x",
                          body_text="b", idempotency_key="k")
    assert_approved_headers(message)               # the builder's own output is the control
    for name in ("Cc", "Bcc", "Reply-To"):
        other = build_reply(in_reply_to=parent, to=("alex@example.com",), subject="Re: x",
                            body_text="b", idempotency_key="k")
        other[name] = "attacker@example.com"
        with pytest.raises(GmailError, match=name):
            assert_approved_headers(other)


def test_finding_02_a_missing_approved_header_is_not_an_extra_one(h):
    """CONTAINMENT, not equality. A digest has no parent, so it carries no `In-Reply-To` and no
    `References` — a legitimate omission. Equality would refuse every digest, which would make this
    a check that refuses the product rather than one that refuses an injection."""
    parent = h.mail.get_thread("t1")[0]
    message = build_reply(in_reply_to=parent, to=("alex@example.com",), subject="Today — x",
                          body_text="b", idempotency_key="k")
    for name in ("In-Reply-To", "References"):
        del message[name]
    assert_approved_headers(message)


def test_finding_02_the_approved_header_set_is_derived_from_the_one_builder():
    """A hand-written tuple of header names is the same defect as a hand-written list of fields to
    check: correct until somebody adds a header, and the gate keeps reporting `held` for a message
    carrying one nobody listed."""
    assert APPROVED_HEADERS == frozenset(reply_header_names())
    assert "Cc" not in APPROVED_HEADERS and "Bcc" not in APPROVED_HEADERS
    assert "In-Reply-To" in APPROVED_HEADERS and "References" in APPROVED_HEADERS


# ============================================================= finding 3 — the calendar ----------
def test_finding_03_the_bound_field_list_is_the_closed_one():
    """One list, so the port and the door-probe table cannot disagree about what "bound" covers."""
    assert CALENDAR_BOUND_FIELDS == ("event_id", "if_match_etag", "start", "end", "location",
                                     "send_updates", "timezone")
    # `to` joined this list in round 8, and the equality is why: the audience the adapter serialises
    # is a bound field too, so a list covering only the envelope keeps reporting "bound" for a
    # header nobody compared.
    assert EMAIL_BOUND_FIELDS == ("to", "in_reply_to", "idempotency_key")


#: One row per calendar field the port reads, with a value the owner never approved. `event_id`
#: moves to ANOTHER MEETING OF THE DAY — the dangerous direction, because that meeting exists and
#: its invite would move. `if_match_etag` is the precondition, so a caller-supplied one is a caller
#: choosing to overwrite a version the owner never saw.
CALENDAR_ATTACKS = {
    "start": at("16:00"),
    "end": at("16:45"),
    "send_updates": "none",
    "if_match_etag": "etag-somebody-elses",
    "event_id": "evt-standup",
    "location": "Somewhere nobody fetched",
}


@pytest.mark.parametrize("field,value", sorted(CALENDAR_ATTACKS.items()))
def test_finding_03_each_engine_field_is_bound_to_the_approved_plan(h, field, value):
    """Each field reached the write from the call. Each is now compared against the approved bundle
    before anything is written."""
    outcome = _split_calendar(h, **{field: value})
    effect = _effect(outcome, "calendar_update")
    assert effect.state == "failed", f"{field} reached the calendar from the call arguments"
    assert "not the value the owner approved" in effect.detail
    assert field in effect.detail, "the refusal does not name the field it refused"
    assert not h.rec.surfaces("calendar")
    assert h.calendar.get_event("ev1").start == at("15:00")


def test_finding_03_control_the_approved_values_still_write(h):
    """The control leg: the approved values in the same places write, so the attack rows above are
    rows about a binding rather than rows about a port that refuses everything."""
    outcome = _split_calendar(h)
    assert h.rec.surfaces("calendar"), (
        f"the control refused too, so the attack rows would prove nothing: "
        f"{_effect(outcome, 'calendar_update').detail}")
    assert h.calendar.get_event("ev1").location == "The Standard, King's Cross"


def test_finding_03_a_write_with_no_approved_plan_to_compare_against_is_refused(h):
    """`change_id` is how the port finds the plan the owner approved. Absent, there is nothing to
    compare against, and "nothing to compare against" is a refusal rather than a pass."""
    outcome = _split_calendar(h, change_id="")
    effect = _effect(outcome, "calendar_update")
    assert effect.state == "failed"
    assert "cannot be matched against the plan" in effect.detail
    assert not h.rec.surfaces("calendar")


# ============================================================= finding 4 — the PARTIAL paths ------
def test_finding_04_a_missing_venue_catalogue_refuses_rather_than_passing(h):
    """`render_venue`/`venue_record` answered `""` when the change carried no catalogue, and an
    empty answer read as clean — so a plan whose research found no venue wrote its `location` with
    nothing having rendered it."""
    change = h.change_resolver.registered["c1"]
    change.venue_catalogue = []
    why = _venue_not_rendered(change, "The Standard, King's Cross")
    assert why, "a missing catalogue was reported clean"
    # The sentence, not the word: the branch below for a catalogue with no such ENTRY also says
    # "catalogue", and a test that accepted either would pass with this guard removed.
    assert "records no venue catalogue" in why


def test_finding_04_a_missing_venue_identity_refuses_rather_than_passing(h):
    """The same rule one branch over: a catalogue that exists but no selected record is a plan whose
    location names nothing. `""` let the caller's raw text through."""
    change = h.change_resolver.registered["c1"]
    change.venue_id = ""
    why = _venue_not_rendered(change, "The Standard, King's Cross")
    assert why, "a missing venue identity was reported clean"
    assert "venue record" in why
    # The control, in the same shape: a record that exists and renders this string is clean.
    assert _venue_not_rendered(change, "") == "", (
        "a write stating no place at all is not this function's business")
    change.venue_id = "v1"
    assert _venue_not_rendered(change, "The Standard, King's Cross") == ""
    assert _venue_not_rendered(change, "Somewhere nobody fetched")


def test_finding_04_the_venue_refusals_reach_the_port(h):
    """The unit above says the helper refuses; this says the PORT asks it. The catalogue is emptied
    AFTER approval, so the approved `location` still matches and this refusal is the one under
    test rather than the field binding's."""
    h.change_resolver.registered["c1"].venue_catalogue = []
    outcome = _split_calendar(h)
    effect = _effect(outcome, "calendar_update")
    assert effect.state == "failed"
    assert "records no venue catalogue" in effect.detail
    assert not h.rec.surfaces("calendar")


def test_finding_04_a_digest_line_must_be_the_render_of_the_record_it_names(h):
    """`record_id` was required and then dropped: the fragment carried an empty `event_id`, so the
    downstream equality check had nothing to compare against and blessed whatever text arrived."""
    with pytest.raises(DigestBadLine, match="not that record's render"):
        compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",),
                       happened=[DigestLine(source="care_happened", record_id="ev1",
                                            text="a sentence ev1 never said")],
                       records={"ev1": "Standup at 09:15"})
    plan = compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",),
                          happened=[DigestLine(source="care_happened", record_id="ev1",
                                               text="Standup at 09:15")],
                          records={"ev1": "Standup at 09:15"})
    happened_frag = next(f for f in plan.fragments if f.source == "care_happened")
    assert happened_frag.event_id == "ev1", (
        "the id was demanded of the line and then dropped from the fragment it renders")
    assert plan.fragments[0].source == "care_greeting"      # the constant opening, not a record


def test_finding_04_the_artifact_id_covers_the_event_id_not_only_the_words():
    """The hash identified a digest by its words alone, so swapping the record a sentence claims to
    have come from left the artifact — and the approval keyed on it — untouched."""
    def plan_for(record_id):
        return compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",),
                              happened=[DigestLine(source="care_happened", record_id=record_id,
                                                   text="Standup at 09:15")],
                              records={record_id: "Standup at 09:15"})
    assert artifact_id(plan_for("ev1")) != artifact_id(plan_for("ev2"))


def test_finding_04_render_venue_refuses_a_record_it_cannot_read():
    """`render_venue` is the engine's own join, and an unreadable record renders nothing rather than
    rendering a blank place the caller can fill in."""
    assert render_venue(None) == ""
    assert VenueUnknown is not None and render_venue({"id": "v1", "name": "The Standard"}) == \
        "The Standard"
