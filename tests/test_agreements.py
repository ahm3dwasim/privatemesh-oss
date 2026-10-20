"""Agreements — what the owner agreed to, and whether the world still matches it.

The product keeps what the owner approved. Nothing until now noticed when the other side later
edited the invite: moved it, swapped the venue, added a guest. These tests are about the two
halves of noticing — that executing a change RECORDS the agreement as approved, and that `check`
compares the world against it without doing anything to the world.

Three claims here are as much the feature as the drift detection is:

* `check` is READ-ONLY. It makes no calendar write, sends no email, asks no model, spends nothing.
  A feature that noticed drift by *acting* on it would be a feature that lets whoever edits an
  invite make this product send mail. The test that holds this claim drives the ROUTE
  (`POST /v2/agreements/check`), because the claim is about what a caller asking the product to
  look gets, not about one function inside it.
* An event's text is untrusted. It is never shown to a model by this feature, which is why the
  zero-model assertion below is a contract rather than an optimisation.
* What is recorded is what the owner APPROVED. A read-back is evidence about the world, and the
  write and the read are two calls with an adversary able to edit the invite in between — so the
  read-back is compared and reported, never adopted as the agreement.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from privatemesh.agreements import (
    DRIFTED,
    HOLDING,
    UNCHECKED,
    Agreement,
    AgreementChecker,
    AgreementStore,
    compare,
)
from privatemesh.api_v2 import build_v2_router
from privatemesh.execute import DONE, UNKNOWN
from privatemesh.sim import SAMPLE_MEETING_ID
from privatemesh.spend import SpendLedger

from fakes_v2 import Harness, at, invite

TZ = "+01:00"


def a_clock(hhmm: str = "09:00"):
    return lambda: dt.datetime.fromisoformat(at(hhmm))


def the_other_side_edits(calendar, event_id: str = "ev1", **fields):
    """Somebody else's client changes the invite.

    Not a call this product makes and not recorded as one: there is no etag precondition to fail,
    because the edit did not come from here. The world simply moved, which is the situation the
    feature exists for."""
    calendar.events[event_id] = dataclasses.replace(calendar.events[event_id], **fields)


def zulu(instant: str) -> str:
    """The same moment in UTC, spelled `Z` — how Google may hand back a write it accepted."""
    return (dt.datetime.fromisoformat(instant).astimezone(dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def executed(h, **over):
    """Run one approved change to completion and return its outcome."""
    cs = h.approved_changeset(**over)
    return h.executor.execute(cs.id)


def v2(h, **kw):
    app = FastAPI()
    app.include_router(build_v2_router(h.v2(**kw)))
    return TestClient(app)


# ---------------------------------------------------------------- recording what was agreed -----
def test_executing_a_change_records_the_agreement_it_executed():
    h = Harness()
    executed(h)

    rows = h.agreements.list()
    assert len(rows) == 1
    row = rows[0]
    assert row.change_id == "c1"
    assert row.event_id == "ev1"
    # The window and the place are the ones the bundle wrote; the title and the guest list are the
    # ones the bundle's own review display carries — the values the owner said yes to. Nothing here
    # is taken from the email, from the plan, or from whatever the provider served afterwards.
    assert row.title == "Alex Doe and You"
    assert (row.start, row.end) == (at("15:00"), at("15:45"))
    assert row.location == "The Standard, King's Cross"
    assert row.attendees == ("alex@example.com",)


def test_a_recorded_agreement_starts_out_holding_because_it_was_just_read_back():
    """The read-back that completes the calendar effect is already a comparison — it is the
    evidence the invite moved. Recording the agreement as `unchecked` would be discarding a
    reading this run just took."""
    h = Harness()
    executed(h)

    row = h.agreements.list()[0]
    assert row.status == HOLDING
    assert row.drift == ()
    assert row.observed is not None
    assert row.checked_at == at("09:00")


def test_a_plan_the_owner_did_not_approve_cannot_move_the_invite_at_all():
    """The seventh review's third finding, at the moment it used to happen.

    This test used to drive the port with a bundle whose window and place were NOT the ones the
    approval was raised over — `10:00–10:30` at `The Warehouse` — and assert the agreement recorded
    them. That is what the port did: `start`, `end`, `location` and `send_updates` reached the
    provider straight off the call's arguments while the approval that authorised the call carried
    the bundle's OWN copy, and nothing compared the two. A plan nobody approved moved the owner's
    meeting, and the row written afterwards described the thing that had happened rather than the
    thing the owner had said yes to.

    So the assertion inverts: the write is refused, and there is no agreement, because there is no
    approved plan for it to be an agreement about. Both halves are checked here — the refusal and
    the absence of the row — because "nothing was recorded" on its own is also what a crash looks
    like.
    """
    h = Harness()
    executed(h, calendar_args=h.calendar_args(start=at("10:00"), end=at("10:30"),
                                              location="The Warehouse"))
    assert h.agreements.list() == []
    assert h.calendar.get_event("ev1").start == at("15:00"), (
        "the meeting moved even though the plan was refused")


def test_a_failed_calendar_effect_records_no_agreement():
    """Nothing moved, so there is nothing the owner agreed to. An agreement recorded here would
    be a promise this product never got the provider to keep."""
    h = Harness()
    h.calendar.fail_update = RuntimeError("provider refused")
    executed(h)
    assert h.agreements.list() == []


def test_an_update_the_provider_accepted_but_did_not_apply_records_no_agreement():
    """`accepted` is not `applied`, which is the whole reason the read-back exists.

    A provider that answers 200 and changes nothing is the case this product already refuses to
    call done. Recording an agreement there would be worse than a wrong effect state: it would put
    a meeting in the owner's list of things they are holding the other side to, on the strength of
    a move that never happened — and the very first `check` would then report it as drift the
    owner never agreed to."""
    h = Harness()
    h.calendar.ignores_update = True
    outcome = executed(h)
    assert outcome.state("calendar_update") == UNKNOWN
    assert h.agreements.list() == []


def test_a_guest_added_between_the_write_and_the_read_back_is_not_recorded_as_agreed():
    """The review's second High, and the whole reason the record is built from the bundle.

    The write and the read-back are two calls to the provider, and anybody with write access to the
    invite can edit it in between. Here that edit happens inside the update: the PATCH lands, a
    stranger is added to the guest list, and only then does the product read the event back. Start,
    end and place all still match, so the old read-back check was satisfied — and the code recorded
    the STRANGER as an attendee the owner had agreed to invite, under status `holding`.

    What the owner agreed to is the reviewed guest list, which is in the bundle
    (`cs.calendar.display`). That is what is recorded. The read-back is compared against it, so the
    addition lands as drift on the spot rather than being signed off as agreement.
    """
    h = Harness()
    real_update = h.calendar.update_event

    def update_then_a_stranger_arrives(event_id, if_match_etag, *, start, end, location,
                                       send_updates, timezone=""):
        landed = real_update(event_id, if_match_etag, start=start, end=end, location=location,
                             send_updates=send_updates, timezone=timezone)
        the_other_side_edits(h.calendar, attendees=("alex@example.com", "stranger@example.com"))
        return landed

    h.calendar.update_event = update_then_a_stranger_arrives
    outcome = executed(h)

    # The effect is still DONE: the invite moved exactly as approved, and that is what the read-back
    # confirms. What changed is who is on it, afterwards.
    assert outcome.state("calendar_update") == DONE
    row = h.agreements.list()[0]
    assert row.attendees == ("alex@example.com",), "the stranger was recorded as agreed"
    assert row.status == DRIFTED
    assert [d["field"] for d in row.drift] == ["attendees"]
    # The owner is shown what the world now reads, so the drift is actionable rather than a bare
    # assertion that something is wrong.
    assert row.observed["attendees"] == ["alex@example.com", "stranger@example.com"]
    assert row.drift[0]["agreed"] == ["alex@example.com"]


def test_a_renamed_event_is_recorded_under_the_title_the_owner_reviewed():
    """The other half of the same High: the title is not an argument of a calendar update (an
    update moves a meeting, it does not rename one), so the only title the owner ever reviewed is
    the one in the bundle's display. Recording the read-back's title instead would let an editor
    racing the write choose the words in the owner's own record.

    The status stays `holding`: `title` is not one of the four compared fields (`docs/API_V2.md`),
    and a meeting somebody renamed has not been moved, re-placed or re-peopled."""
    h = Harness()
    real_update = h.calendar.update_event

    def update_then_the_invite_is_renamed(event_id, if_match_etag, *, start, end, location,
                                          send_updates, timezone=""):
        landed = real_update(event_id, if_match_etag, start=start, end=end, location=location,
                             send_updates=send_updates, timezone=timezone)
        the_other_side_edits(h.calendar, summary="Totally different meeting")
        return landed

    h.calendar.update_event = update_then_the_invite_is_renamed
    executed(h)

    row = h.agreements.list()[0]
    assert row.title == "Alex Doe and You"
    assert row.observed["start"] == at("15:00")
    assert row.status == HOLDING


def test_a_read_back_in_another_offset_still_records_the_agreement():
    """The review's fourth finding: the prerequisite that decides whether an agreement is recorded
    at all compared times as STRINGS.

    Google may serve `14:00:00Z` for the `15:00:00+01:00` it accepted — the same moment written the
    other way. The drift comparator already knew that; the read-back check did not, so a write that
    landed exactly as approved became `unknown` and left no agreement behind. The two comparisons
    have to be one rule, or the product refuses to remember a move it just made.
    """
    h = Harness()
    real_update = h.calendar.update_event

    def update_and_serve_in_utc(event_id, if_match_etag, *, start, end, location, send_updates,
                                timezone=""):
        return real_update(event_id, if_match_etag, start=zulu(start), end=zulu(end),
                           location=location, send_updates=send_updates, timezone=timezone)

    h.calendar.update_event = update_and_serve_in_utc
    outcome = executed(h)

    assert outcome.state("calendar_update") == DONE
    row = h.agreements.list()[0]
    # What is recorded as agreed is the window the owner approved, in the form the bundle wrote it,
    # with the provider's spelling kept beside it as what the world reads.
    assert (row.start, row.end) == (at("15:00"), at("15:45"))
    assert row.observed["start"] == zulu(at("15:00"))
    assert row.status == HOLDING
    assert row.drift == ()


# ---------------------------------------------------------------- comparing the world ----------
def test_a_moved_event_is_drifted_on_the_times():
    h = Harness()
    executed(h)
    the_other_side_edits(h.calendar, start=at("17:00"), end=at("17:30"))

    rows = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()
    row = rows[0]
    assert row.status == DRIFTED
    assert {d["field"] for d in row.drift} == {"start", "end"}
    assert row.drift[0] == {"field": "start", "agreed": at("15:00"), "observed": at("17:00")}
    assert row.observed["start"] == at("17:00")


def test_a_swapped_venue_is_drifted_on_the_location():
    h = Harness()
    executed(h)
    the_other_side_edits(h.calendar, location="Somewhere else entirely")

    row = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert row.status == DRIFTED
    assert [d["field"] for d in row.drift] == ["location"]


def test_an_added_guest_is_drifted_on_the_attendees():
    h = Harness()
    executed(h)
    the_other_side_edits(h.calendar, attendees=("alex@example.com", "stranger@example.com"))

    row = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert row.status == DRIFTED
    assert [d["field"] for d in row.drift] == ["attendees"]
    assert row.drift[0]["observed"] == ["alex@example.com", "stranger@example.com"]


def test_a_removed_guest_is_drifted_too():
    h = Harness()
    h.calendar.events["ev1"] = dataclasses.replace(
        h.calendar.events["ev1"], attendees=("alex@example.com", "dana@example.com"))
    executed(h)
    the_other_side_edits(h.calendar, attendees=("alex@example.com",))

    row = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert row.status == DRIFTED
    assert [d["field"] for d in row.drift] == ["attendees"]


def test_an_unchanged_event_holds():
    h = Harness()
    executed(h)
    row = AgreementChecker(h.agreements, h.calendar, now=a_clock("11:00")).check()[0]
    assert row.status == HOLDING
    assert row.drift == ()
    assert row.note == ""
    assert row.checked_at == at("11:00")


def test_a_deleted_event_is_unchecked_and_says_why():
    """`unchecked` is not `holding`. A meeting nobody can read is not a meeting that still
    matches, and reporting it as one would be the product inventing good news."""
    h = Harness()
    executed(h)
    del h.calendar.events["ev1"]

    row = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert row.status == UNCHECKED
    assert row.observed is None
    assert row.drift == ()
    assert "ev1" in row.note


def test_one_unreadable_event_does_not_stop_the_others_being_checked():
    """A check that gave up on the first failure would report "nothing could be looked at" over a
    calendar where exactly one meeting had gone — and the owner would lose the drift they could
    actually have been told about."""
    h = Harness()
    executed(h)
    h.agreements.save(Agreement(id="agr-gone", change_id="c9", event_id="ev-does-not-exist",
                                title="Cancelled meeting", start=at("09:00"), end=at("09:30"),
                                location="", attendees=()))
    the_other_side_edits(h.calendar, start=at("17:00"))

    rows = {a.id: a for a in AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()}
    assert rows["agr-gone"].status == UNCHECKED
    assert rows["agr-ev1"].status == DRIFTED


def test_an_agreement_never_checked_is_unchecked_and_says_so():
    h = Harness()
    h.agreements.save(Agreement(id="agr-x", change_id="c9", event_id="ev1", title="Older meeting",
                                start=at("15:00"), end=at("15:45"), location="", attendees=()))
    row = h.agreements.list()[0]
    assert row.status == UNCHECKED
    assert row.note != ""


def test_emails_compare_as_a_case_insensitive_set():
    """A guest list is a set of addresses. Order is an artefact of how the provider serialises it
    and case is an artefact of how somebody typed it; neither is the other side changing the
    meeting, and a feature that cried wolf on either would be turned off."""
    agreed = Agreement(id="a", change_id="c", event_id="e", title="t", start=at("15:00"),
                       end=at("15:45"), location="", attendees=("Alex@Example.com",
                                                               "dana@example.com"))
    event = dataclasses.replace(invite(), attendees=("dana@example.com", "alex@example.com"),
                                start=at("15:00"), end=at("15:45"), location="")
    status, _observed, drift = compare(agreed, event)
    assert (status, drift) == (HOLDING, [])


def test_emails_compare_caselessly_and_not_merely_ascii_lowercase():
    """The review's Low: "case-insensitive" was `lower()`, which is only caseless for the ASCII
    alphabet. `straße@example.com` and `STRASSE@example.com` are one address written two ways under
    Unicode caseless matching, and the feature reported the difference as drift — a false alarm
    about a change nobody made, which is the failure mode this comparison exists to avoid."""
    agreed = Agreement(id="a", change_id="c", event_id="e", title="t", start=at("15:00"),
                       end=at("15:45"), location="", attendees=("straße@example.com",))
    event = dataclasses.replace(invite(), attendees=("STRASSE@example.com",),
                                start=at("15:00"), end=at("15:45"), location="")
    assert compare(agreed, event)[0] == HOLDING


def test_times_compare_as_instants_not_as_strings():
    """`15:00+01:00` and `14:00Z` are the same moment written two ways, and a provider that
    switches between them has not moved the meeting."""
    same = at("15:00")
    agreed = Agreement(id="a", change_id="c", event_id="e", title="t", start=same,
                       end=at("15:45"), location="", attendees=())
    event = dataclasses.replace(invite(), start="2026-09-19T14:00:00Z",
                                end="2026-09-19T14:45:00Z", location="", attendees=())
    assert compare(agreed, event)[0] == HOLDING


def test_a_later_agreement_supersedes_the_one_it_replaces():
    """One agreement per event, not one per execution: the second move is what the owner has now
    agreed to, and keeping the first alongside it would report the superseded window as drift
    forever."""
    h = Harness()
    executed(h)
    the_other_side_edits(h.calendar, start=at("17:00"), end=at("17:30"))
    cs = h.approved_changeset(calendar_args=h.calendar_args(start=at("17:00"), end=at("17:30")))
    h.executor.execute(cs.id)

    rows = h.agreements.list()
    assert len(rows) == 1
    assert (rows[0].start, rows[0].end) == (at("17:00"), at("17:30"))
    assert AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0].status == HOLDING


def test_a_check_that_raced_a_newer_agreement_cannot_restore_the_superseded_one():
    """The review's first High, at the moment it happens.

    `check` reads an agreement, goes out to the provider, and writes back what it found. That read
    takes time. Here the owner approves and runs a SECOND move of the same meeting inside that
    window, so a newer agreement exists by the time the check comes to write — and the check is
    holding an answer about the record it read, which is no longer the owner's agreement.

    Writing it would restore the superseded window permanently: every later check compares against
    that baseline, so the resurrection is not a transient wrong answer but the new truth. The write
    is a compare-and-swap against the revision the check read, and it is dropped.
    """
    h = Harness()
    executed(h)
    the_other_side_edits(h.calendar, start=at("17:00"), end=at("17:30"))
    # `record` moved the row on when the execution wrote it; the check reads THAT revision.
    assert h.agreements.list()[0].revision == 1

    real_read, fired = h.calendar.get_event, []

    def read_then_the_owner_moves_it_again(event_id):
        event = real_read(event_id)
        if not fired:
            fired.append(True)
            cs = h.approved_changeset(
                calendar_args=h.calendar_args(start=at("17:00"), end=at("17:30")))
            h.executor.execute(cs.id)
        return event

    h.calendar.get_event = read_then_the_owner_moves_it_again
    rows = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()
    assert fired, "the race this test is about never happened"

    stored = h.agreements.get("agr-ev1")
    assert (stored.start, stored.end) == (at("17:00"), at("17:30")), "the stale check overwrote it"
    assert stored.status == HOLDING
    assert stored.drift == ()
    assert stored.revision == 2
    # And the check ANSWERS with the record that survived, not with the reading it took: the result
    # of a dropped write describes an agreement the owner no longer has.
    assert (rows[0].start, rows[0].end) == (at("17:00"), at("17:30"))
    assert rows[0].status == HOLDING


def test_a_check_that_raced_nothing_still_writes_and_moves_the_revision_on():
    """The mutation control for the swap above: without a race the check persists what it saw.

    A compare-and-swap that dropped every write would satisfy the test above for the worst possible
    reason, and this is the half that says so."""
    h = Harness()
    executed(h)
    the_other_side_edits(h.calendar, start=at("17:00"), end=at("17:30"))

    row = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert row.status == DRIFTED
    assert row.revision == 2
    assert h.agreements.get("agr-ev1").status == DRIFTED


# ---------------------------------------------------------------- the check does nothing -------
def test_the_check_route_writes_nothing_sends_nothing_and_asks_no_model():
    """The strongest form of the read-only claim, taken through the ROUTE the claim is about.

    The review's Medium: this used to call `AgreementChecker.check()` directly, so the guarantee
    was established for one function while being quoted for `POST /v2/agreements/check`. Anything
    the product does on the way — a model call, an email, a calendar write added in
    `V2Service.check_agreements` or in the wiring above it — would have been invisible here and
    still invisible to the route test, which asserts only that drift comes back.

    So the whole request path is driven: router, service, checker, store. A real `SpendLedger` is
    attached to the same Token Factory the engine holds, so "zero spend" is measured against the
    ledger that would have been charged rather than inferred from "no model call happened" — and a
    check that did reach a model would leave a row in it.
    """
    h = Harness()
    executed(h)
    the_other_side_edits(h.calendar, start=at("17:00"), end=at("17:30"))
    h.tf.tf.spend = SpendLedger(path=":memory:")
    api = v2(h, mode="sample", authenticated_owner=True)
    before, before_model = list(h.rec.entries), len(h.tf.calls)

    body = api.post("/v2/agreements/check").json()

    assert h.rec.entries == before
    assert h.tf.calls == h.tf.calls[:before_model]
    assert h.tf.tf.spend.totals()["total_usd"] == 0.0
    # And the check DID look: a feature that read nothing would report nothing and pass this test
    # for the wrong reason. Drift is the evidence it read, and the GET serves the same answer
    # because the route persisted what it saw.
    assert body["agreements"][0]["status"] == DRIFTED
    assert api.get("/v2/agreements").json()["agreements"][0]["status"] == DRIFTED


# ---------------------------------------------------------------- the API ----------------------
def test_agreements_are_served_owner_gated():
    h = Harness()
    executed(h)
    api = v2(h, mode="sample", authenticated_owner=True)
    body = api.get("/v2/agreements").json()
    assert set(body) == {"agreements"}
    row = body["agreements"][0]
    # `note` is absent rather than empty: an agreement that holds has nothing to explain, and an
    # empty string would render as a note the owner has to read and dismiss.
    assert set(row) == {"id", "change_id", "event_id", "title", "status", "agreed", "observed",
                        "drift", "checked_at"}
    assert row["status"] == HOLDING
    assert row["drift"] == []
    assert row["agreed"] == {"start": at("15:00"), "end": at("15:45"),
                             "location": "The Standard, King's Cross",
                             "attendees": ["alex@example.com"]}


def test_the_check_route_re_reads_the_world_and_reports_drift():
    h = Harness()
    executed(h)
    api = v2(h, mode="sample", authenticated_owner=True)
    the_other_side_edits(h.calendar, start=at("17:00"), end=at("17:30"))

    body = api.post("/v2/agreements/check").json()
    assert body["agreements"][0]["status"] == DRIFTED
    assert body["agreements"][0]["drift"][0]["field"] == "start"
    # And the GET now answers the same thing, because the check persisted what it saw.
    assert api.get("/v2/agreements").json()["agreements"][0]["status"] == DRIFTED


def test_an_unreadable_event_is_reported_as_unchecked_with_a_note():
    h = Harness()
    executed(h)
    api = v2(h, mode="sample", authenticated_owner=True)
    del h.calendar.events["ev1"]

    row = api.post("/v2/agreements/check").json()["agreements"][0]
    assert row["status"] == UNCHECKED
    assert row["observed"] is None
    assert row["note"]


def test_a_record_this_build_cannot_read_is_served_not_omitted(tmp_path):
    """The review's third finding: a corrupt row turned the owner's list into an empty one.

    `scan()` detected the unreadable payload, logged it, kept its id in `store.unreadable` — and
    then answered with the readable rows only, while the API ignored `unreadable` entirely. A store
    holding nothing but a truncated payload served `{"agreements": []}`: the one answer that is
    indistinguishable from "you have no agreements", over a record that is still on disk.

    Both routes serve it now, as an `unchecked` agreement whose note names the record and the
    failure (`docs/API_V2.md`). `unchecked` is not invented for the occasion — it is this module's
    word for "nothing here has compared it", and nothing has.
    """
    h = Harness()
    executed(h)
    h.agreements._conn.execute("INSERT OR REPLACE INTO agreements(id, payload) VALUES(?, ?)",
                               ("agr-broken", "{not json"))
    h.agreements._conn.commit()
    api = v2(h, mode="sample", authenticated_owner=True)

    for response in (api.get("/v2/agreements"), api.post("/v2/agreements/check")):
        rows = {r["id"]: r for r in response.json()["agreements"]}
        assert set(rows) == {"agr-ev1", "agr-broken"}, "the unreadable record was omitted"
        broken = rows["agr-broken"]
        assert broken["status"] == UNCHECKED
        assert broken["observed"] is None
        assert broken["drift"] == []
        assert "could not be read" in broken["note"]
        assert "JSONDecodeError" in broken["note"]
    # The record itself is untouched, which is the other half of the store's contract: a row this
    # build cannot read is never overwritten with state derived from nothing.
    stored = h.agreements._conn.execute("SELECT payload FROM agreements WHERE id=?",
                                        ("agr-broken",)).fetchone()["payload"]
    assert stored == "{not json"


def test_a_readable_store_serves_its_rows_and_no_ghosts():
    """The mutation control for the test above: nothing is invented when nothing is broken."""
    h = Harness()
    executed(h)
    rows = h.agreements.rows()
    assert [r.id for r in rows] == ["agr-ev1"]
    assert not any(r.corrupt for r in rows)


def test_unauthenticated_calls_in_live_mode_are_refused():
    h = Harness()
    executed(h)
    api = v2(h, mode="live", authenticated_owner=False, owner_token="s3cret")
    assert api.get("/v2/agreements").status_code == 403
    assert api.post("/v2/agreements/check").status_code == 403
    # With the credential the same route answers, so the 403 is the gate and not a broken route.
    assert api.get("/v2/agreements", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_agreements_survive_a_restart_through_the_sample_composition(tmp_path):
    """The whole point of remembering an agreement is that the process that made it is gone — and
    the claim is about the COMPOSITION, so the test has to build one rather than a bare store.

    The review's Low is that this used to construct `Harness` directly, which constructs
    `AgreementStore(path=...)` itself: `wiring._build` could have been changed to `:memory:` or to
    a different filename and this test would still have passed, because it was testing its own
    wiring rather than the product's.

    A row the seeder does not know about is the discriminator. Both builds seed the sample's two
    fixtures, so a second build reproduces those from the day whatever the store does — but nothing
    invents `agr-restart`, and only a store actually rooted in this directory has it.
    """
    from privatemesh.wiring import build_sample

    first = build_sample(path=str(tmp_path))
    first.agreements.save(Agreement(
        id="agr-restart", change_id="change-that-only-the-first-build-saw",
        event_id=SAMPLE_MEETING_ID, title="Written before the restart", start=at("15:00"),
        end=at("15:45"), location="The Standard, King's Cross",
        attendees=("alex@example.com",), checked_at=at("09:00")))

    second = build_sample(path=str(tmp_path))
    row = second.agreements.get("agr-restart")
    assert row is not None, "the agreement did not survive the restart"
    assert row.change_id == "change-that-only-the-first-build-saw"
    assert row.attendees == ("alex@example.com",)
    # And the product's own data-directory file is where it lives, not a store that happens to be
    # in the same process.
    assert (tmp_path / "agreements.db").exists()


# ---------------------------------------------------------------- the sample --------------------
def test_the_sample_serves_one_holding_and_one_drifted_agreement():
    from privatemesh.wiring import build_sample

    wiring = build_sample()
    app = FastAPI()
    app.include_router(build_v2_router(wiring.service))
    api = TestClient(app)

    rows = api.get("/v2/agreements").json()["agreements"]
    assert {r["status"] for r in rows} == {HOLDING, DRIFTED}
    assert len(rows) == 2
    drifted = next(r for r in rows if r["status"] == DRIFTED)
    assert [d["field"] for d in drifted["drift"]] == ["start", "end"]

    # The sample's check route agrees with the fixtures it was seeded with.
    again = api.post("/v2/agreements/check").json()["agreements"]
    assert {r["status"] for r in again} == {HOLDING, DRIFTED}


def test_the_sample_agreement_fixtures_track_the_sample_day():
    """The holding fixture is built from the day's own events rather than from copy-pasted
    strings, so it cannot survive a change to the sample day and quietly mean something else."""
    from privatemesh.sim import SAMPLE_MEETING_ID, SAMPLE_DATE, _sample_events
    from privatemesh.sim import sample_agreements

    events = {e.event.id: e.event for e in _sample_events(SAMPLE_DATE)}
    rows = {a.event_id: a for a in sample_agreements(SAMPLE_DATE)}
    alex = rows[SAMPLE_MEETING_ID]
    assert (alex.title, alex.start, alex.end, alex.location, alex.attendees) == (
        events[SAMPLE_MEETING_ID].summary, events[SAMPLE_MEETING_ID].start,
        events[SAMPLE_MEETING_ID].end, events[SAMPLE_MEETING_ID].location,
        events[SAMPLE_MEETING_ID].attendees)
    dana = rows["evt-client-call"]
    assert (dana.start, dana.end) != (events["evt-client-call"].start,
                                      events["evt-client-call"].end)


@pytest.mark.parametrize("status", [HOLDING, DRIFTED, UNCHECKED])
def test_every_status_round_trips_through_the_store(status):
    h = Harness()
    h.agreements.save(Agreement(id="agr-1", change_id="c1", event_id="ev1", title="t",
                                start=at("15:00"), end=at("15:45"), location="here",
                                attendees=("a@example.com",), status=status,
                                checked_at=at("09:00")))
    assert h.agreements.list()[0].status == status
    assert h.agreements.get("agr-1").status == status


# ------------------------------------------------- the round-2 review, finding by finding -------
# Every test below was written against the SHA the review was made on and seen RED there. The
# findings they hold are numbered as the review numbers them.


def a_stored_row(**over) -> Agreement:
    """One agreement with everything the store needs, overridable field by field."""
    fields = dict(id="agr-ev1", change_id="c1", event_id="ev1", title="Our usual cafe has closed",
                  start=at("15:00"), end=at("15:45"), location="The Standard, King's Cross",
                  attendees=("alex@example.com",), checked_at=at("09:00"))
    fields.update(over)
    return Agreement(**fields)


def approved_for(calendar, event_id: str = "ev1", **over) -> dict:
    """What `approved_view` returns, built from the event as it now reads."""
    event = calendar.get_event(event_id)
    fields = dict(event_id=event_id, title=event.summary, start=event.start, end=event.end,
                  location=event.location, attendees=tuple(str(a) for a in event.attendees))
    fields.update(over)
    return fields


# ---- New-1: the CAS is ONE statement, so two connections cannot both win ----------------------
def test_two_connections_cannot_both_write_the_same_revision(tmp_path, monkeypatch):
    """New-1 High. Two server workers, two connections, both holding revision 1.

    Both read the row, both decide their write is the one that supersedes it, and — under a save
    that reads and then writes unconditionally — both succeed, the later one silently discarding
    the other. The per-instance Python lock the review found here cannot cover this: the second
    writer is in another process, or at least in another store.

    The interleave is FORCED rather than hoped for. The two writers are parked inside the read,
    with neither having written, and only then released; every run therefore decides the same
    question. Whether the write that follows is conditional on the row still being the bytes the
    decision was made about is exactly what the two outcomes below differ by.
    """
    import threading

    from privatemesh import agreements as agreements_module

    db = str(tmp_path / "agreements.db")
    first, second = AgreementStore(path=db), AgreementStore(path=db)
    first.save(a_stored_row())
    assert first.list()[0].revision == 1
    assert second.list()[0].revision == 1, "the second worker did not see the stored agreement"

    barrier = threading.Barrier(2, timeout=10)
    real_load = agreements_module._load
    parked = threading.local()

    def load_together(payload, row_id=None):
        # Spelled to work against both readings of "the read step": this build's `_load` takes the
        # row's key as a required argument, and the build the review measured took only the payload.
        parsed = real_load(payload) if row_id is None else real_load(payload, row_id)
        if threading.current_thread() is not threading.main_thread():
            if not getattr(parked, "done", False):
                parked.done = True
                barrier.wait()          # both have read revision 1; neither has written yet
        return parsed

    monkeypatch.setattr(agreements_module, "_load", load_together)

    results, failures = {}, {}

    def racer(name, store, start):
        def run():
            try:
                results[name] = store.save(a_stored_row(start=start), expect=1)
            except BaseException as exc:                              # noqa: BLE001
                failures[name] = exc
        return run

    threads = [threading.Thread(target=racer("first", first, at("16:00"))),
               threading.Thread(target=racer("second", second, at("17:00")))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not failures, failures
    winners = [name for name, saved in results.items() if saved is not None]
    assert len(winners) == 1, (
        f"both connections wrote revision 2 and one write was silently lost: {results}")
    stored = first.list()[0]
    assert stored.revision == 2, "the winner's write did not land"
    assert stored.start == results[winners[0]].start, (
        "the stored agreement is not the one whose write reported success")


# ---- New-2: an older EXECUTION may not replace a newer one's agreement ------------------------
def test_an_older_execution_cannot_replace_a_newer_agreements_record():
    """New-2 High, at the moment it happens.

    The window is not the check's. It is between an execution's read-back and its record: the
    invite has moved, the write is confirmed, and the run is a few statements from writing down
    what the owner agreed to. A SECOND execution — approved and started after this one — moves the
    same meeting again and records its agreement inside that window. The first run then resumes
    holding the window the world has already left.

    Revisions cannot see this: the older run's write is the LATER write, so it would carry the
    higher revision and win on every revision comparison there is. What separates them is the
    order the executions STARTED, taken before the provider was touched.
    """
    h = Harness()
    executed(h)                                    # the meeting is at 15:00 and that is recorded
    # The older run's bundle is approved FIRST, before any of this: its place in the order is
    # fixed by when it starts, and nothing here may move it.
    cs = h.approved_changeset(calendar_args=h.calendar_args(start=at("16:00"), end=at("16:30")))
    real_read, fired = h.calendar.get_event, []

    def read_then_the_owner_moves_it_again(event_id):
        event = real_read(event_id)
        # The read-back of the older run's OWN write — not the re-read `_admit` takes before the
        # provider is touched, which is why this waits for the window that only exists afterwards.
        if not fired and event.start == at("16:00"):
            fired.append(True)
            cs = h.approved_changeset(
                calendar_args=h.calendar_args(start=at("17:00"), end=at("17:30")))
            h.executor.execute(cs.id)              # a NEWER run, all the way to its record
        return event

    h.calendar.get_event = read_then_the_owner_moves_it_again
    h.executor.execute(cs.id)                      # the older run, resumed after the newer one

    assert fired, "the race this test is about never happened"
    rows = h.agreements.list()
    assert len(rows) == 1
    assert (rows[0].start, rows[0].end) == (at("17:00"), at("17:30")), (
        "the older execution put its superseded window back as the owner's agreement")
    # And the world it would have been compared against agrees with the newer window, which is why
    # this is the record that had to survive rather than a matter of taste.
    assert AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0].status == HOLDING


@pytest.mark.parametrize("order", ["newer first", "older first"])
def test_the_newer_execution_wins_whichever_order_the_two_records_land(order):
    """New-2, both orders, at the store — the half the execution test above cannot vary.

    The sequence is taken when a run STARTS, so "newer first" is the racing case and "older first"
    is the ordinary one. Both must end with the newer run's window stored; a rule that only held
    for one of them would be a rule about who wrote last.
    """
    h = Harness()
    older, newer = h.agreements.begin_execution(), h.agreements.begin_execution()
    assert newer > older, "the store handed out its sequence numbers out of order"

    def record(start, end, sequence, change_id):
        the_other_side_edits(h.calendar, start=start, end=end)
        return h.agreements.record(change_id=change_id, approved=approved_for(h.calendar, start=start,
                                                                            end=end),
                                   event=h.calendar.get_event("ev1"), sequence=sequence)

    runs = {"older": (at("15:00"), at("15:30"), older, "c-old"),
            "newer": (at("17:00"), at("17:30"), newer, "c-new")}
    for name in (["newer", "older"] if order == "newer first" else ["older", "newer"]):
        record(*runs[name])

    rows = h.agreements.list()
    assert len(rows) == 1
    assert (rows[0].start, rows[0].end) == (at("17:00"), at("17:30"))
    assert rows[0].change_id == "c-new"


# ---- New-3: a bundle that never named its guests records none of them -------------------------
def test_a_bundle_that_never_named_its_guests_records_no_guests_from_the_read_back():
    """New-3 Medium. The review's scenario: the bundle has no reviewed attendee list, and somebody
    adds a guest between the write and the read-back.

    The read-back is the one value in this pair that an editor racing the write controls, so the
    guest list must not come from it. It cannot come from the bundle either — the bundle never
    wrote one down. What is left is the honest answer: nothing has compared this, and it says why.
    """
    import json

    h = Harness()
    the_other_side_edits(h.calendar, attendees=("alex@example.com", "stranger@example.com"))
    row = h.agreements.record(change_id="c1",
                              approved=approved_for(h.calendar, attendees=None),
                              event=h.calendar.get_event("ev1"))

    assert row.attendees == (), "the read-back's guest list was recorded as the owner's agreement"
    assert row.status == UNCHECKED
    assert row.observed is None
    assert "did not record who" in row.note
    assert "stranger@example.com" not in json.dumps(row.as_dict())
    # A check does not turn it into an answer either: there is still nothing to compare against.
    checked = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert checked.status == UNCHECKED and "did not record who" in checked.note


def test_a_notifying_bundle_that_names_nobody_cannot_be_approved():
    """New-3, the other half: `docs/API_V2.md` has always said this is refused, and nothing did.

    A bundle that will email the guests and does not say who they are is the only way `record` can
    be handed no reviewed list at all, so the refusal is what keeps the branch above from being the
    normal case rather than the last resort.
    """
    from privatemesh.changeset import StaleChangeSet

    h = Harness()
    cs = h.pending_changeset(calendar_display={"event_id": "ev1", "title": "t", "diff": {},
                                               "notifies_attendees": True, "notifies": None})
    with pytest.raises(StaleChangeSet) as refusal:
        h.changesets.approve(cs.id, cs.version,
                             current_event_etag=h.calendar.get_event("ev1").etag,
                             current_boundary_version=h.boundary.version_of("pc1"))
    assert "does not say who they are" in str(refusal.value)

    # A plan that tells nobody is a different thing and is still approvable: it has not left the
    # guest list to be discovered, it has said there is no notification at all.
    quiet = h.pending_changeset(calendar_args=h.calendar_args(send_updates="none"),
                                calendar_display={"event_id": "ev1", "title": "t", "diff": {},
                                                  "attendees": None, "notifies_attendees": False,
                                                  "notifies": []})
    assert h.changesets.approve(
        quiet.id, quiet.version, current_event_etag=h.calendar.get_event("ev1").etag,
        current_boundary_version=h.boundary.version_of("pc1")).status == "approved"


# ---- New-4: the row key is bound to the payload's own id --------------------------------------
def test_a_row_whose_key_disagrees_with_its_payload_is_corrupt_not_served_as_the_payload(tmp_path):
    """New-4 Medium. Row `agr-disguised` carries a payload that names itself `agr-ev1`.

    Served, it is a second copy of an agreement the owner has, and checking it writes its payload
    over the real `agr-ev1`. The key is the row's identity and the payload's `id` is a claim made
    inside the row; a record where the two disagree is not a record of either name.
    """
    import sqlite3

    from privatemesh.agreements import UnreadableAgreement, _dump

    store = AgreementStore(path=str(tmp_path / "agreements.db"))
    store.save(a_stored_row())
    real = store.get("agr-ev1")
    assert real is not None and real.status == UNCHECKED

    conn = sqlite3.connect(str(tmp_path / "agreements.db"))
    conn.execute("INSERT INTO agreements(id, payload) VALUES(?, ?)",
                 ("agr-disguised", _dump(a_stored_row(change_id="c-disguised"))))
    conn.commit()
    conn.close()

    rows = {row.id: row for row in store.rows()}
    assert set(rows) == {"agr-ev1", "agr-disguised"}, (
        "the disguised row was served under the id its payload claims")
    assert rows["agr-disguised"].corrupt
    assert rows["agr-disguised"].status == UNCHECKED
    assert "could not be read" in rows["agr-disguised"].note
    # The real row is untouched, and the disguised one is never written through.
    assert rows["agr-ev1"].change_id == "c1" and not rows["agr-ev1"].corrupt
    with pytest.raises(UnreadableAgreement):
        store.save(a_stored_row(id="agr-disguised", start=at("17:00")))


# ---- New-5: a store failure after the effect landed does not abort the run --------------------
def test_a_broken_agreement_store_does_not_abort_an_execution_that_already_moved_the_invite(tmp_path):
    """New-5 Medium. The calendar effect is `DONE` before the agreement is recorded.

    A malformed row already occupies this event's agreement id, so the record raises — and under
    the behaviour the review found, that exception leaves `Executor.execute` entirely: no reply is
    sent, no outcome is persisted, and the bundle stays `executing` over an invite that really did
    move. The external effect happened; the run has to finish and say so.
    """
    import sqlite3

    h = Harness(data_dir=str(tmp_path))
    conn = sqlite3.connect(str(tmp_path / "agreements.db"))
    conn.execute("INSERT INTO agreements(id, payload) VALUES(?, ?)", ("agr-ev1", "{not json"))
    conn.commit()
    conn.close()

    # The abort is turned into an assertion rather than left as a traceback. Both are red, but only
    # this one says WHAT went wrong, and only this one is evidence the mutation runner can credit:
    # it requires the named test to have failed on an assertion, and an exception escaping the
    # executor is exactly the failure this test exists to catch.
    try:
        outcome = executed(h)
    except Exception as exc:                                          # noqa: BLE001
        raise AssertionError(
            f"the agreement store's failure escaped the execution: {type(exc).__name__}: {exc}"
        ) from exc

    assert outcome.state("calendar_update") == DONE
    assert outcome.state("email_reply") == DONE, "the reply was abandoned after the invite moved"
    assert outcome.summary == "Invite updated. Reply sent."
    # The failure is where the owner reads failures, and the agreement is served as what it is.
    note = [row for row in h.decisions.for_change("c1") if row["stage"] == "agreement"]
    assert note, "the agreement failure is not in the decision log"
    assert note[0]["verdict"] == UNCHECKED
    assert "recorded after the effect completes" in note[0]["rule"]
    served = {row.id: row for row in h.agreements.rows()}["agr-ev1"]
    assert served.status == UNCHECKED and served.note


# ---- New-6: a record from a newer build is unreadable, never downgraded ------------------------
@pytest.mark.parametrize("newer", [
    # Each case is pinned to ONE guard by holding the others still. The version has to be stated
    # for the first two rather than defaulted, or a helper that bumps it to 2 for them means all
    # three cases are refused by the version check and neither the status nor the field check is
    # exercised by any test — which is what this test did until a mutant of the status check stayed
    # green and said so.
    {"status": "accepted_changed", "v": 1},
    {"evidence": {"provider": "a build that knows more than this one"}, "v": 1},
    {"v": 99},
])
def test_a_record_from_a_newer_build_is_unreadable_and_is_never_rewritten(tmp_path, newer):
    """New-6 Medium. A newer build's record read as a subset of itself, then written back, is that
    record deleted — the downgrade is destructive and permanent.

    Three shapes, one rule. An unknown status is not "unchecked" (that is a claim about this build,
    not about the record); an unknown field is something this build cannot carry forward; a later
    schema version says so out loud. A status is a value, not a schema, so an unknown one at the
    schema this build reads is the case that has nothing but the status check to refuse it.
    """
    import json
    import sqlite3

    path = str(tmp_path / "agreements.db")
    store = AgreementStore(path=path)
    store.save(a_stored_row(status=HOLDING))
    conn = sqlite3.connect(path)
    payload = json.loads(conn.execute("SELECT payload FROM agreements WHERE id='agr-ev1'")
                         .fetchone()[0])
    payload.update(newer)
    if "v" not in newer:
        payload["v"] = 2                       # a build that added a field also bumped the schema
    conn.execute("UPDATE agreements SET payload=? WHERE id='agr-ev1'",
                 (json.dumps(payload),))
    conn.commit()
    before = conn.execute("SELECT payload FROM agreements WHERE id='agr-ev1'").fetchone()[0]
    conn.close()

    served = {row.id: row for row in store.rows()}["agr-ev1"]
    assert served.corrupt and served.status == UNCHECKED, (
        "a newer build's record was served as this build's own")
    assert "could not be read" in served.note

    # A check runs over it and must not write the downgrade back over the newer record.
    calendar = Harness().calendar
    AgreementChecker(AgreementStore(path=path), calendar, now=a_clock()).check()
    conn = sqlite3.connect(path)
    after = conn.execute("SELECT payload FROM agreements WHERE id='agr-ev1'").fetchone()[0]
    conn.close()
    assert after == before, "the newer build's record was rewritten by this build"


def test_agreements_survive_a_restart_through_the_LIVE_composition(tmp_path):
    """New-8 / round-1 Low: the restart test above says "live composition" and builds the SAMPLE.

    `build_sample` and `build_live` both end in `wiring._build`, but they reach it differently —
    `build_live` is the one that turns `data_dir` into the directory the stores are rooted in, and
    it is the one a regression would hit: `directory = Path(data_dir)` could be dropped, so a live
    product keeps its agreements in `:memory:` while the sample test stays green, because the
    sample's own path argument still works. The connectors here are the fakes
    `tests/test_wiring_live.py` drives the real `GmailClient`/`GCalClient` through, so this is the
    live composition with no network rather than a second sample.
    """
    from test_gcal import EVENT_ID
    from test_wiring_live import build as build_the_live_product, fresh_token

    first, _ = build_the_live_product(tmp_path, token=fresh_token())
    assert first.mode == "live", "this test is about the live composition"
    first.agreements.save(Agreement(
        id="agr-live-restart", change_id="change-that-only-the-first-build-saw",
        event_id=EVENT_ID, title="Written before the restart", start=at("15:00"),
        end=at("15:45"), location="The Standard, King's Cross",
        attendees=("alex@example.com",), checked_at=at("09:00")))

    second, _ = build_the_live_product(tmp_path, token=fresh_token())
    row = second.agreements.get("agr-live-restart")
    assert row is not None, "the agreement did not survive a live restart"
    assert row.change_id == "change-that-only-the-first-build-saw"
    assert row.attendees == ("alex@example.com",)
    assert (tmp_path / "agreements.db").exists(), (
        "the live composition did not root its agreements in the data directory")


# ------------------------------------------------- the round-3 review, finding by finding ------
# Written against 773e3e9, the SHA the review was made on, and seen RED there. Numbered as the
# review numbers them: two Highs, and the Medium and Low the review listed for the next version.


def test_a_third_execution_colliding_twice_does_not_lose_to_the_older_ones(tmp_path, monkeypatch):
    """Round-3 High 1. `save` made exactly TWO attempts, so the third writer's record was dropped.

    The review's interleave, forced rather than hoped for, because whether it happens at all is
    otherwise a matter of timing. Three executions have all moved the same meeting and are all
    about to write down what the owner agreed to. The newest reads the row; execution 1 replaces it
    before the newest one's conditional update can commit, so that write is refused; the newest
    re-reads and execution 2 replaces the row again before its second update. Out of attempts, the
    newest falls back to the row on disk — sequence 2, which is not the newest, and whose window
    the world has already left.

    An attempt budget cannot be right here, because the number of writers is not a constant this
    store knows: any fixed cap is a rule that the (cap + 1)th writer loses, and the (cap + 1)th
    writer is the newest one. What ends the loop instead is the row itself — our write landing, or
    a stored row from an execution at or after ours, which is the one case where dropping the write
    is the correct answer.
    """
    db = str(tmp_path / "agreements.db")
    colliding, first, second = (AgreementStore(path=db) for _ in range(3))
    colliding.save(a_stored_row())                       # the baseline, before any execution
    seq_first, seq_second, seq_newest = (colliding.begin_execution() for _ in range(3))
    assert (seq_first, seq_second, seq_newest) == (1, 2, 3), (
        "the store handed out its sequence numbers out of order")

    real_read_row = colliding._read_row
    # The two older executions, each parked so its write lands INSIDE the newest one's window: after
    # the row has been read and the decision made about it, and before the conditional update that
    # decision authorises can commit.
    older_runs = [(first, seq_first, at("16:00")), (second, seq_second, at("16:30"))]
    landed = []

    def read_then_an_older_execution_writes(agreement_id):
        row = real_read_row(agreement_id)
        if len(landed) < len(older_runs):
            store, sequence, start = older_runs[len(landed)]
            landed.append(sequence)
            store.save(a_stored_row(start=start), newer_than=sequence)
        return row

    monkeypatch.setattr(colliding, "_read_row", read_then_an_older_execution_writes)

    result = colliding._store(a_stored_row(start=at("15:00")), seq_newest)

    assert landed == [seq_first, seq_second], (
        f"the two collisions this test is about never happened: {landed}")
    assert result is not None and result.sequence == seq_newest, (
        "the newest execution was handed back the record of an older run: the store gave up after "
        f"{len(landed) + 1} attempts and returned sequence "
        f"{None if result is None else result.sequence}, not {seq_newest}")
    stored = colliding.get("agr-ev1")
    assert stored.sequence == seq_newest, (
        f"the row left on disk is sequence {stored.sequence}, not the newest run's {seq_newest}")
    assert stored.start == at("15:00"), (
        "the newest run's window lost to an older run that merely wrote last")


def test_a_run_that_cannot_be_numbered_does_not_touch_the_calendar(monkeypatch):
    """Round-3 High 2. `_begin_sequence` returned None and the run carried on unnumbered.

    A sequence is what makes "the newer execution wins" true, and `newer_than=None` makes the
    recording unconditional. So a store that could not hand one out did not merely lose the
    ordering, it REMOVED the protection: the run went on to move the invite and write its record
    over whatever a newer execution had already recorded. The review's scenario is exactly that —
    one execution times out on the sequence lock, a newer one records, and the first resumes with
    `sequence=None` and overwrites it.

    Failing closed costs the owner a run and nothing else. The invite is not touched, the reply is
    not sent, the change is not executed, and the reason is in both places the owner reads: the
    decision log, and the run's own report of what it did not do.
    """
    import sqlite3

    from privatemesh.changeset import STOPPED_STATUS

    h = Harness()
    cs = h.approved_changeset()
    invite_before = dataclasses.replace(h.calendar.get_event("ev1"))

    def no_sequence_can_be_taken():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(h.agreements, "begin_execution", no_sequence_can_be_taken)

    outcome = h.executor.execute(cs.id)

    assert h.rec.surfaces("calendar") == [], "the run wrote to the calendar without a sequence"
    assert h.mail.sent == [], "the run sent the reply without a sequence"
    assert h.calendar.get_event("ev1") == invite_before, "the invite moved without a sequence"

    assert outcome.state("calendar_update") != DONE, (
        f"a run that was never numbered reported a moved invite: {outcome.summary}")
    assert outcome.state("email_reply") != DONE
    assert h.changesets.latest(cs.id).status == STOPPED_STATUS, (
        "the change did not settle as one that was not executed")
    assert h.agreements.list() == [], (
        "a run that never happened left an agreement behind")

    refused = [row for row in h.decisions.for_change(cs.change_id)
               if row["stage"] == "execute" and row["verdict"] == "refused"]
    assert refused, "the refusal is not in the decision log"
    assert "order of executions" in refused[0]["subject"]
    assert "cannot overwrite a newer run's agreement" in refused[0]["rule"], (
        "the decision row does not say why an unnumbered run may not proceed")
    # The detail is deliberately not asserted on: this harness builds its log without a guard, and
    # an unguarded log withholds every detail by design (`DecisionLog._scrub` — "no guard means no
    # detail"). The reason the owner reads is the rule, which is written by the engine.


def _a_recording_that_fails(monkeypatch, h):
    """The store that just failed, on the path where the invite has already moved."""
    import sqlite3

    def record_fails(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(h.agreements, "record", record_fails)


def test_a_fallback_row_keeps_the_guest_list_the_owner_actually_reviewed(monkeypatch):
    """Round-3 Medium. The fallback row wrote `attendees=()` and `unreviewed=False`.

    `()` is not "unknown" in this store — it is the claim that the owner agreed to NO guests, and
    the next check measures the calendar against it. So a first recording that failed and a
    fallback that succeeded turned a guest list the owner reviewed into a drifted meeting with
    strangers on it: the owner is shown a guest list they never agreed to, and a warning about a
    difference that does not exist. The fallback is the row that carries the failure, not a licence
    to invent the baseline.
    """
    h = Harness()
    _a_recording_that_fails(monkeypatch, h)

    outcome = executed(h)

    assert outcome.state("calendar_update") == DONE, "the invite never moved; nothing to fall back to"
    row = h.agreements.get("agr-ev1")
    assert row is not None, "the fallback row was not written at all"
    assert row.status == UNCHECKED and row.error, "the fallback row does not carry the failure"
    assert row.attendees == ("alex@example.com",), (
        "the fallback row claims the owner agreed to a guest list they never reviewed: "
        f"{row.attendees}")
    assert row.unreviewed is False, (
        "a bundle that DID name the people it would notify was recorded as unreviewed")
    # And the consequence, which is what the Medium is actually about: the next check must not
    # report drift against a baseline nobody agreed to.
    checked = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert checked.status != DRIFTED, (
        f"a failed recording produced false drift on the next check: {checked.drift}")


def test_a_fallback_row_for_a_bundle_that_named_nobody_is_unreviewed(monkeypatch):
    """Round-3 Medium, the other half: where there is no reviewed list there is no baseline.

    A bundle that told nobody is approvable without a guest list, so the fallback for one has no
    list to keep. Writing `attendees=()` with `unreviewed=False` there is the same false claim in a
    louder form — it asserts the owner reviewed an empty list, so every later check compares the
    real guests against nothing and reports drift that no decision of the owner's produced.
    """
    h = Harness()
    _a_recording_that_fails(monkeypatch, h)
    quiet = dict(calendar_args=h.calendar_args(send_updates="none"),
                 calendar_display={"event_id": "ev1", "title": "t", "diff": {},
                                   "attendees": None, "notifies_attendees": False,
                                   "notifies": []})

    outcome = executed(h, **quiet)

    assert outcome.state("calendar_update") == DONE
    row = h.agreements.get("agr-ev1")
    assert row is not None, "the fallback row was not written at all"
    assert row.attendees == (), "the read-back's guest list was recorded as the owner's agreement"
    assert row.unreviewed is True, (
        "a fallback row for a bundle that named nobody claims a guest list the owner reviewed")
    checked = AgreementChecker(h.agreements, h.calendar, now=a_clock()).check()[0]
    assert checked.status == UNCHECKED and "did not record who" in checked.note


@pytest.mark.parametrize("stored", ["false", 0, 1, [], {}])
def test_a_stored_unreviewed_that_is_not_a_boolean_is_unreadable(stored, tmp_path):
    """Round-3 Low. `unreviewed` was coerced with `bool(...)`, so `"false"` read as True.

    `unreviewed` decides whether a row is checked at all, so a value this build did not write must
    not be interpreted as a decision about that: `"false"` meant the row was skipped by every check
    for as long as it stayed on disk, silently, rather than being reported as the unreadable record
    it is. The rest of the store's stance is that a payload this build cannot read is SERVED as
    unreadable and never overwritten, and this field is now held to it.
    """
    import json

    h = Harness()
    executed(h)
    row = h.agreements._conn.execute("SELECT payload FROM agreements WHERE id=?",
                                     ("agr-ev1",)).fetchone()
    payload = json.loads(row["payload"])
    assert payload["unreviewed"] is False, "this test's fixture is not the shape it assumes"
    payload["unreviewed"] = stored
    rewritten = json.dumps(payload)
    h.agreements._conn.execute("UPDATE agreements SET payload=? WHERE id=?", (rewritten, "agr-ev1"))
    h.agreements._conn.commit()

    served = {r.id: r for r in h.agreements.rows()}["agr-ev1"]
    assert served.corrupt, (
        f"a stored `unreviewed` of {stored!r} was read as a decision about whether to check this "
        f"row (unreviewed={served.unreviewed!r}) instead of being reported as unreadable")
    assert served.status == UNCHECKED
    # The record is served, not repaired: the untouched bytes are the only evidence of what was
    # there, and a build that "fixed" this field would be overwriting somebody else's record.
    still = h.agreements._conn.execute("SELECT payload FROM agreements WHERE id=?",
                                       ("agr-ev1",)).fetchone()["payload"]
    assert still == rewritten, "the unreadable record was rewritten rather than reported"


def test_a_row_written_before_the_unreviewed_field_existed_still_reads(tmp_path):
    """The control for the test above: refusing what this build did not write is not refusing
    everything. `revision` and `sequence` are read as 0 when absent for the same reason — a field
    that is simply not there is not a field carrying a value this build cannot interpret."""
    import json

    h = Harness()
    executed(h)
    payload = json.loads(h.agreements._conn.execute(
        "SELECT payload FROM agreements WHERE id=?", ("agr-ev1",)).fetchone()["payload"])
    del payload["unreviewed"]
    h.agreements._conn.execute("UPDATE agreements SET payload=? WHERE id=?",
                               (json.dumps(payload), "agr-ev1"))
    h.agreements._conn.commit()

    served = {r.id: r for r in h.agreements.rows()}["agr-ev1"]
    assert not served.corrupt, "a row that simply lacks the field was reported as unreadable"
    assert served.unreviewed is False


def test_a_swap_that_never_wins_raises_rather_than_reporting_a_dropped_write(tmp_path, monkeypatch):
    """The other end of the retry loop, and the reason it is a deadline and not a try count.

    `save` returning `None` is not a neutral answer in this store: it means "an older write
    correctly lost to a newer one", and callers act on it — `_store` answers with the row on disk
    and `record` reports that row as the agreement. A caller handed `None` because the clock ran out
    would therefore be told its agreement was superseded when it was never written at all, which is
    the same class of mistake as the High this loop replaces, made from the other side. So the
    deadline RAISES, and the raise arrives at callers as any other store failure does.
    """
    from privatemesh.agreements import AgreementStoreBusy

    db = str(tmp_path / "agreements.db")
    # A zero deadline: the loop is allowed exactly one attempt, so the test is about which answer
    # comes out of a losing swap, not about how long the test is willing to wait for one.
    store = AgreementStore(path=db, cas_timeout=0.0)
    other = AgreementStore(path=db)
    store.save(a_stored_row())

    real_read_row = store._read_row
    rounds = []

    def a_row_that_never_stops_changing(agreement_id):
        """Every read is followed by somebody else's write, so this swap can never land."""
        row = real_read_row(agreement_id)
        rounds.append(True)
        other.save(a_stored_row(start=at("16:00") if len(rounds) == 1 else at("16:30")))
        return row

    monkeypatch.setattr(store, "_read_row", a_row_that_never_stops_changing)

    with pytest.raises(AgreementStoreBusy) as busy:
        store.save(a_stored_row(start=at("15:00")), newer_than=store.begin_execution())

    assert rounds, "the collision this test is about never happened"
    assert "not made" in str(busy.value), (
        f"the refusal does not say the write did not happen: {busy.value}")
