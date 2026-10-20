"""The headline: across a FULL run, no protected field reaches any provider.

This is the product's central claim, so it is asserted the way the direction demands — a unique
synthetic marker in each of title, location, reason and attendees, checked against every captured
provider-bound payload: every model call, every search query, the calendar write and the email.

Two things make the assertion worth something:

* each field is asserted on its own, so removing one marker while leaking another still fails;
* the run is also asserted to have actually SENT things — "nothing left the machine" would pass a
  marker test while proving nothing — and a blinded-gate control shows the harness can see a leak
  when one exists.
"""
from __future__ import annotations

import pytest

from privatemesh.boundary import EgressRefused, PrivacyGate

from fakes_v2 import Harness, at, change_email, evidence

MARKERS = {
    "title": "PC_TITLE_Marker_School pickup for Ada",
    "location": "PC_LOCATION_Marker_42 Conservatory Lane",
    "reason": "PC_REASON_Marker_Ada piano recital",
    "attendee": "pc_marker_attendee_ada@example.com",
}

COMMITMENT = {"title": MARKERS["title"], "location": MARKERS["location"],
              "reason": MARKERS["reason"], "attendees": (MARKERS["attendee"],)}

SCRIPT = {
    "classify": {"is_meeting_change": True, "reason": "proposes new times"},
    "extract": {"options": [{"start": at("16:00"), "end": at("16:45"), "phrase": "4"},
                            {"start": at("15:00"), "end": at("15:45"), "phrase": "3"}],
                "request_excerpt": "Could we meet near King's Cross tomorrow at 3 or 4?"},
    "query": [{"query": "cafe near King's Cross open in the afternoon"},
              {"query": "The Standard King's Cross opening hours"}],
    "choose": {"venue_index": 0, "is_venue": True,
               "reason_sentence": "It is open at 15:00.", "rejected": []},
    "draft": {"subject": "Re: Our usual cafe has closed",
              "template_body": "Hi Alex,",
              "fragments": [{"source": "time", "text": "15:00-15:45 works for me."},
                            {"source": "venue", "text": "The Standard, King's Cross."}]},
}


def harness(**kw):
    kw.setdefault("messages", [change_email("m1")])
    kw.setdefault("commitment", COMMITMENT)
    kw.setdefault("script", SCRIPT)
    kw.setdefault("search", {"king's cross": [
        evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
                 "Open daily 12:00-23:00")]})
    return Harness(**kw)


def full_run(h):
    change = h.engine.detect(None)[0]
    prepared = h.engine.prepare(change.id)
    assert prepared.stage == "proposal_ready"
    cs = h.changesets.latest(change.id)
    h.changesets.approve(cs.id, cs.version,
                         current_event_etag=h.calendar.get_event("ev1").etag,
                         current_boundary_version=h.boundary.version_of("pc1"))
    outcome = h.executor.execute(cs.id)
    return change, cs, outcome


@pytest.mark.parametrize("field", sorted(MARKERS))
def test_no_protected_field_reaches_any_provider_in_a_full_run(field):
    h = harness()
    full_run(h)
    blob = h.rec.text().lower()
    assert MARKERS[field].lower() not in blob, \
        f"the protected {field} reached {h.rec.find(MARKERS[field])}"


def test_the_run_actually_reached_the_providers_it_is_cleared_to_reach():
    """Without this the marker assertions above could pass on a run that did nothing at all."""
    h = harness()
    change, cs, outcome = full_run(h)
    assert outcome.summary == "Invite updated. Reply sent."
    assert len(h.rec.surfaces("model")) >= 4
    assert h.rec.surfaces("search") and h.rec.surfaces("calendar") and h.rec.surfaces("mail")
    assert "16:30" in h.rec.text("model"), "the allowed planning fact never reached the model"


def test_the_outbound_email_carries_no_protected_field_either():
    h = harness()
    full_run(h)
    sent = h.rec.surfaces("mail")[0]["payload"]
    for marker in MARKERS.values():
        assert marker.lower() not in (sent["subject"] + sent["body"]).lower()
    assert "15:00" in sent["body"]


def test_the_calendar_write_carries_no_protected_field():
    h = harness()
    full_run(h)
    call = h.rec.surfaces("calendar")[0]["payload"]
    # `timezone` joined the payload in round 8: the adapter used to add it itself, so the field that
    # decides how the provider reads the approved instant was the one field nobody approved. The
    # equality is kept — this list is what leaves, and a field appearing here without an approval
    # behind it is the finding, not the assertion's problem.
    assert set(call) == {"event_id", "if_match", "start", "end", "location", "send_updates",
                         "timezone"}
    for marker in MARKERS.values():
        assert marker.lower() not in str(call).lower()


def test_an_incoming_thread_that_quotes_the_protected_reason_is_refused_before_the_model():
    """A leak can arrive from outside; the boundary refuses to forward it rather than passing it
    along inside a prompt nobody re-reads."""
    quoted = change_email("m1", body=f"Our usual cafe has closed. Shall we still do the "
                                     f"{MARKERS['reason']}? 3 or 4 near King's Cross?")
    h = harness(messages=[quoted])
    with pytest.raises(EgressRefused) as exc:
        h.engine.detect(None)
    assert "refused" in str(exc.value)
    assert not h.rec.surfaces("model"), "the refused call still posted a body"
    assert MARKERS["reason"].lower() not in h.rec.text().lower()


def test_the_harness_can_see_that_leak_when_the_gate_is_blinded():
    """Negative control at engine level: with the gate unable to list the protected values, the
    same message puts the quoted reason into a captured model payload."""
    quoted = change_email("m1", body=f"Shall we still do the {MARKERS['reason']}? 3 or 4?")
    h = harness(messages=[quoted])
    h.gate.protected_values = lambda: []          # the blinding, and only the blinding
    h.engine.detect(None)
    assert h.rec.find(MARKERS["reason"], "model"), "the control cannot see a leak it should see"


def test_the_gate_reads_every_current_commitment_not_just_the_first():
    h = harness()
    h.boundary.put(id="pc2", start=at("11:00"), end=at("12:00"), buffer_minutes=15,
                   title="SECOND_MARKER_Title", location="SECOND_MARKER_Location")
    gate = PrivacyGate(h.boundary)
    with pytest.raises(Exception):
        gate.check_payload({"x": "SECOND_MARKER_Location"}, kind="model")
