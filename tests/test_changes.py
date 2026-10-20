"""The flow: detect a change, read it, reject 16:00, research a venue, offer one bundle.

The engine is the place where the private constraint has to change the ANSWER and not merely be
absent from the screen, so these tests look at the decision: which option survived, on what
evidence, and what the model was actually asked.
"""
from __future__ import annotations

import pytest

from privatemesh.boundary import ProtectedLeak, SchemaLeak

from fakes_v2 import Harness, at, busy, change_email, evidence

TIMES = [{"start": at("16:00"), "end": at("16:45"), "phrase": "4"},
         {"start": at("15:00"), "end": at("15:45"), "phrase": "3"}]


def script(**over):
    base = {
        "classify": {"is_meeting_change": True, "reason": "proposes new times"},
        "extract": {"options": TIMES,
                    "request_excerpt": "Could we meet near King's Cross tomorrow at 3 or 4?"},
        "query": [{"query": "cafe near King's Cross"},
                  {"query": "The Standard King's Cross opening hours",
                   "include_domains": ["thestandard.co.uk"]}],
        "choose": {"venue_index": 0, "is_venue": True,
                   "reason_sentence": "It is open at 15:00.",
                   "rejected": [{"index": 1, "reason": "closed at 15:00"}]},
        "draft": {"subject": "Re: Our usual cafe has closed",
                  "template_body": "Hi Alex,",
                  "fragments": [{"source": "time", "text": "15:00-15:45 works for me."},
                                {"source": "venue", "text": "The Standard, King's Cross."}]},
    }
    base.update(over)
    return base


@pytest.fixture
def h():
    return harness()


def harness(**kw):
    kw.setdefault("messages", [change_email("m1")])
    kw.setdefault("search", {"king's cross": [
        evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
                 "Open daily 12:00-23:00", official=True)]})
    return Harness(script=script(**kw.pop("script", {})), **kw)


def test_detect_finds_the_meeting_change(h):
    changes = h.engine.detect(None)
    assert [c.message_id for c in changes] == ["m1"]
    assert changes[0].from_address == "alex@example.com"
    assert changes[0].stage == "detected"


def test_detect_ignores_an_email_that_is_not_a_meeting_change():
    h = harness(script={"classify": {"is_meeting_change": False, "reason": "a newsletter"}})
    assert h.engine.detect(None) == []


#: One slot only, so `no_safe_fit` can only be about the busy list and the error names the reason
#: the two tests below are actually arguing about. The default script offers 16:00 as well, and
#: 16:00 always loses to the buffer, which would mask what these two are testing.
ONLY_1500 = {"extract": {"options": [{"start": at("15:00"), "end": at("15:45"), "phrase": "3"}],
                         "request_excerpt": "Could we meet near King's Cross tomorrow at 3?"}}


def test_a_meeting_can_be_moved_onto_a_slot_that_overlaps_where_it_already_is():
    """A meeting is not booked against itself.

    Free/busy answers with every occupied interval, the moved event's own block included. Left in
    that set, it refuses every candidate that overlaps where the meeting already sits — so a
    second request for the same move, or an owner nudging a meeting by fifteen minutes, comes back
    `no_safe_fit` about a part of the day that is in fact free. The harness's event is the
    15:00–15:45 meeting Alex wrote about, so the provider's answer here is the meeting itself.
    """
    h = harness(script=ONLY_1500, busy=[busy("15:00", "15:45")])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    assert change.proposal["start"] == at("15:00")
    assert change.proposal["end"] == at("15:45")


def test_an_edit_that_overlaps_where_the_meeting_already_is_is_allowed():
    """The owner's own edit goes through the same validator, so it has the same blind spot.

    The meeting being moved is 15:00–15:45. Asked for 14:30 it would run 14:30–15:15, which
    overlaps the 15:00–15:45 block the meeting itself occupies — so the edit came back "overlaps
    something already booked" about the very event it was moving, and the owner could not nudge a
    meeting earlier. Nothing else is booked in this harness, so the meeting is the only thing that
    can refuse it.
    """
    h = harness(script=ONLY_1500, busy=[busy("15:00", "15:45")])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    revised = h.engine.revise(change.id, start=at("14:30"))
    assert revised.stage == "proposal_ready"
    assert revised.proposal["start"] == at("14:30")
    assert revised.proposal["end"] == at("15:15")


def test_a_block_that_only_overlaps_the_moved_meeting_still_refuses_the_slot():
    """The other half of the rule: the exclusion is by exact window, not by overlap.

    15:15–15:30 is not the meeting being moved — it is somebody else's commitment that happens to
    sit inside it. Dropping it because it merely touches the event would double-book the owner, so
    the slot has to stay refused. This is the control for the test above: same script, same slot,
    only the busy window differs, and it goes red if the filter is widened to "overlaps".
    """
    h = harness(script=ONLY_1500, busy=[busy("15:15", "15:30")])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "no_safe_fit"
    assert "already booked" in change.error


def test_prepare_rejects_1600_and_offers_1500(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready"
    assert change.proposal["start"] == at("15:00")
    assert change.proposal["end"] == at("15:45")
    rejected = {r["phrase"]: r for r in change.proposal["rejected"]}
    assert "4" in rejected
    assert "15:45" in rejected["4"]["reason"], "the rejection does not name the boundary"


def test_the_rejected_option_carries_a_reason_the_owner_can_check(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    reason = next(r["reason"] for r in change.proposal["rejected"] if r["phrase"] == "4")
    assert "16:30" in reason and "buffer" in reason.lower()


def test_the_proposal_shows_a_researched_venue_with_its_evidence(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    venue = change.proposal["venue"]
    assert venue["name"] == "The Standard, King's Cross"
    assert venue["evidence"]["url"].startswith("https://thestandard.co.uk")
    assert venue["evidence"]["retrieved_at"]
    assert "seating not reserved" in venue["note"]


def test_research_is_bounded_to_two_rounds_and_uses_the_first_round_to_replan(h):
    h.engine.prepare(h.engine.detect(None)[0].id)
    assert len(h.search.queries) == 2, h.search.queries
    assert "king's cross" in h.search.queries[1].lower()


def test_a_venue_with_no_official_source_keeps_its_uncertainty(h):
    # `venue_index: None` is what a model shown NO results can honestly answer, and since round 6
    # it is also the only answer available: an index the engine never fetched is a refusal rather
    # than a quiet "no venue" (`VenueUnknown`). The claim this test makes is about the note, and it
    # is unchanged — an unverified venue is proposed as unverified.
    h = harness(search={}, script={"choose": {"venue_index": None, "is_venue": False,
                                              "reason_sentence": "No pages came back.",
                                              "rejected": []}})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.proposal["venue"]["evidence"] is None
    assert "not verified" in change.proposal["venue"]["note"]
    assert change.proposal["needs_choice"] is True


def test_a_day_with_no_safe_fit_says_so_instead_of_inventing_one():
    h = harness(script={"extract": {"options": [TIMES[0]], "request_excerpt": "4?"}})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "no_safe_fit"
    assert change.proposal is None
    assert not h.rec.surfaces("mail")


def test_the_proposal_binds_both_effects_through_the_broker(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    cs = h.changesets.latest(change.id)
    assert cs is not None and cs.status == "pending"
    for action in cs.actions():
        approval = h.approvals.get(action.approval_id)
        assert approval is not None and approval.status == "pending"
    kinds = {a["kind"] for a in cs.as_dict()["actions"]}
    assert kinds == {"calendar_update", "email_reply"}


def test_the_email_action_is_the_exact_reviewed_text(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    cs = h.changesets.latest(change.id)
    assert cs.email.arguments["to"] == ["alex@example.com"]
    assert cs.email.arguments["body"].startswith("Hi Alex,")
    assert "15:00-15:45" in cs.email.arguments["body"]
    assert len(cs.email.arguments["idempotency_key"]) >= 8


def test_the_proposal_carries_the_privacy_line_built_only_from_the_allowed_fact(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.proposal["privacy_line"] == (
        "Your 16:30 commitment stays protected. Its details are not included.")
    assert change.proposal["protected"]["label"] == "Protected"


def test_the_model_is_given_the_allowed_fact_and_never_the_commitment(h):
    h.engine.prepare(h.engine.detect(None)[0].id)
    asked = h.rec.text("model")
    assert "16:30" in asked and "protected" in asked
    for marker in ("PC_TITLE", "PC_LOCATION", "PC_REASON", "pc_attendee"):
        assert marker not in asked, f"the model was shown {marker}"


def test_the_change_exposes_the_why_disclosure(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    why = h.engine.why(change.id)
    assert why["used"], "nothing was disclosed as used"
    assert set(why["kept_private"]["categories"]) >= {"title", "location", "reason", "attendees"}
    assert why["technical"]["model_calls"] >= 3
    assert all(isinstance(a, int) or isinstance(a, str) for a in why["technical"]["audit_ids"])


def test_leave_marks_the_change_and_leaves_the_calendar_alone(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    h.engine.leave(change.id)
    assert h.engine.get(change.id).stage == "stopped"
    assert not h.rec.surfaces("calendar") and not h.mail.sent


def test_a_draft_that_reaches_for_a_protected_source_never_becomes_a_proposal(h):
    """The reply is composed from allowlisted fragments; an unlisted source stops the flow."""
    h = harness(script={"draft": {"subject": "Re: tomorrow",
                                  "fragments": [{"source": "protected_reason",
                                                 "text": "something private"}]}})
    change = h.engine.detect(None)[0]
    with pytest.raises(SchemaLeak):
        h.engine.prepare(change.id)
    assert not h.rec.surfaces("mail"), "a refused draft still reached a provider"


def test_a_reply_that_paraphrases_the_private_reason_never_becomes_a_proposal(h):
    h = harness(script={"draft": {
        "subject": "Re: tomorrow",
        "fragments": [{"source": "template",
                       "text": "I have a piano recital at the conservatory that afternoon."}]}})
    change = h.engine.detect(None)[0]
    with pytest.raises((ProtectedLeak, SchemaLeak, Exception)):
        h.engine.prepare(change.id)
    assert not h.rec.surfaces("mail")


def test_the_extraction_is_repaired_once_when_the_model_returns_junk(h):
    h = harness(script={"extract": ["not json at all", {"options": TIMES,
                                                        "request_excerpt": "3 or 4?"}]})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready"
    assert h.tf.phases().count("extract") == 2


TWO_CAFES = {"king's cross": [
    evidence("https://thestandard.co.uk/", "The Standard, King's Cross", "Open daily 12:00-23:00"),
    evidence("https://cafezwei.example/", "Cafe Zwei", "Open 09:00-18:00")]}


def test_an_edit_can_choose_the_other_venue_the_research_found():
    """An edit is a new version with new arguments, not a renamed field: the calendar location
    and both approvals have to move with it."""
    h = Harness(script=script(choose={"venue_index": 0, "is_venue": True,
                                      "reason_sentence": "It is open at 15:00.",
                                      "rejected": [{"index": 1, "reason": "quieter"}]}),
                messages=[change_email("m1")], search=TWO_CAFES)
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.proposal["venue"]["name"] == "The Standard, King's Cross"
    first = h.changesets.for_change(change.id)

    revised = h.engine.revise(change.id, venue_index=1)

    assert revised.version == first.version + 1
    assert revised.proposal["venue"]["name"] == "Cafe Zwei"
    # The address is not in the search result, so the location is the name alone rather than a
    # guess at a street.
    assert h.changesets.for_change(change.id).calendar.arguments["location"] == "Cafe Zwei"
    assert any(r["name"] == "The Standard, King's Cross" for r in revised.proposal["rejected"])
    assert h.approvals.get(first.calendar.approval_id).status != "pending"


def test_an_edit_to_a_venue_that_was_never_researched_is_refused():
    h = harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    with pytest.raises(LookupError):
        h.engine.revise(change.id, venue_index=3)
    assert h.changesets.for_change(change.id).version == 1


def test_a_moved_meeting_keeps_its_own_length_whatever_the_model_guessed():
    """The first live run: real Nemotron read "3 or 4" as two ONE-HOUR slots, 15:00-16:00 and
    16:00-17:00. 15:00-16:00 runs into the buffer before 16:30, so a perfectly good 15:00 came back
    `no_safe_fit`. The sender offered start times; the meeting being moved is 45 minutes. Its
    length is a calendar fact, not a model guess."""
    hour_long = {"extract": {"options": [
        {"start": at("16:00"), "end": at("17:00"), "phrase": "4"},
        {"start": at("15:00"), "end": at("16:00"), "phrase": "3"}],
        "request_excerpt": "Could we meet near King's Cross tomorrow at 3 or 4?"}}
    h = harness(script=hour_long)
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    assert change.proposal["start"] == at("15:00")
    assert change.proposal["end"] == at("15:45")
