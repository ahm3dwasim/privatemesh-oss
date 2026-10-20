"""Round 6: one rule, every port, every field.

The rule the round was given: **every outbound field, at every port, is either an engine-rendered
fragment carrying the event id it derives from, or the action is refused** — and **the day a guard
reasons about comes from trusted state, never from caller input**.

Four doors were open when the round started, and each test here is about one of them. They are
written to fail first: every one names the line whose removal turns it red (see
`redteam/mutcheck_findings.py --care-r6`), so none of them is a test that can only pass.

  FINDING 1  the email port sent `args["body"]` without asking whether any fragment declared it,
             and checked the day's other meetings against the body while the SUBJECT went out
             unread.
  FINDING 2  `_day_window` took its offset from the proposal's own timestamp, so a `-12:00` stamp
             moved the queried window half a day, the foreign set came back empty, and emptiness
             read as innocence.
  FINDING 3  the calendar `location` was raw model text: the choose answer's `name`/`address` went
             straight to the invite. The model must SELECT from what the engine fetched.
  FINDING 4  `compose_digest` demanded a `record_id` and then dropped it, so every digest fragment
             carried an empty event id and an unrendered line passed the body-equality check.
"""
from __future__ import annotations

import json

import pytest

from privatemesh.boundary import (CalendarRefused, ForeignEventLeak, SchemaLeak,
                                  foreign_fragments_of)
from privatemesh.care import DigestBadLine, DigestLine, compose_digest
from privatemesh.changes import VenueUnknown, venue_catalogue, venue_address_in
from privatemesh.changeset import APPROVED_STATUS
from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL
from privatemesh.sim import SAMPLE_DATE

from fakes_v2 import Harness, at, change_email, evidence, invite

from test_changes import harness
from test_redteam_guards import _approved, _email_effect

FOREIGN = "Your Team standup at 09:15 moves too."


def _two_meetings():
    return [invite(), invite("evt-standup", start="09:15", end="09:30", etag="etag-standup")]


def _pin(h):
    change = h.change_resolver.registered["c1"]
    change.event_pinned, change.plan_day = "ev1", SAMPLE_DATE
    change.proposal = {"start": at("15:00")}
    return change


# ============================================================ finding 1 — the email port ----------
def test_finding_01_a_body_no_fragment_declares_is_refused_at_the_port():
    """The port judges the message the change CLAIMS to be, not the arguments it was handed.

    The reviewed build ran `check_text` over `args["body"]` and then sent it. Every check it ran was
    a check on a value it had taken on trust: nothing asked whether a fragment had declared those
    bytes, so a caller that is not the planner could put any sentence it liked on the wire.

    The mutant this is written against replaces the port's fragments with a fragment built from the
    body itself (`_outbound_from_bundle`), which makes `body == rendered()` a tautology — the
    reviewed shape — and this test then fails on the first assertion.
    """
    h = Harness(events=_two_meetings())
    _pin(h)
    cs = _approved(h, body="Hi Alex, I have moved the standup to 09:15 as well.")
    assert cs.status == APPROVED_STATUS, "the fixture failed to approve, so nothing was tested"

    outcome = h.executor.execute(cs.id)

    assert h.mail.sent == [], "a body no fragment declared reached the mail port"
    assert "not the render of its allowlisted fragments" in (_email_effect(outcome).detail or "")

    # THE CONTROL: the same bundle, the same pin, the body the change DOES declare. It is sent, so
    # this is not a port that refuses everything.
    good = Harness(events=_two_meetings())
    _pin(good)
    cs = _approved(good)                       # `email_args()` renders `c1`'s decomposition exactly
    good.executor.execute(cs.id)
    assert [m["to"] for m in good.mail.sent] == [["alex@example.com"]], \
        "the port refused the reply the engine itself composed"


def test_finding_01_a_foreign_subject_is_refused_even_when_the_body_is_clean():
    """The half the reviewed check could not see: it read the body and never the subject.

    A subject is a header a recipient reads. The reviewed build handed `args["subject"]` straight to
    the provider after scanning only `args["body"]`, so a bundle whose body was the engine's own
    render and whose subject named another meeting sent the name of that meeting.

    The mutant this is written against is the reviewed call: `guard_foreign_events(body)`, with the
    subject left out of the string — the run then sends, and the first assertion fails.
    """
    h = Harness(events=_two_meetings())
    change = _pin(h)
    # The subject IS the change's own declared render — the thread's subject, spelled the way the
    # engine would have written it — and it names another meeting. That is the case the reviewed
    # check could not see, and it is deliberately NOT a subject the caller made up: since the
    # SEVENTH review's first finding a subject no source declared is refused one check earlier, so a
    # hand-written subject would make this test pass for the wrong reason and leave the foreign-event
    # question unasked. Written this way, the render equality holds and the only thing that can
    # refuse the send is the foreign check reading the header.
    change.reply_subject_fragments = [{"source": "thread_subject", "text": FOREIGN}]
    cs = _approved(h, subject=f"Re: {FOREIGN}")
    assert cs.status == APPROVED_STATUS, "the fixture failed to approve, so nothing was tested"

    outcome = h.executor.execute(cs.id)

    assert h.mail.sent == [], "a subject naming another meeting reached the mail port"
    detail = _email_effect(outcome).detail or ""
    assert "evt-standup" in detail, f"the refusal does not name the meeting: {detail}"
    assert "Team standup" not in detail, "the refusal quotes the text that revealed it"


def test_finding_01_the_exemption_does_not_excuse_a_sentence_the_correspondent_never_wrote():
    """`exempt_text` narrows the foreign set, so it needs its own control.

    The exemption is "the words of the message this change is about", and the reason it is sound is
    that the reply goes back to the person who wrote that message: their own sentence is not a
    disclosure to them. It is NOT a licence to describe a second meeting — a fragment that is not in
    that message stays in the foreign set and is refused. Without this test, `exempt_text` could
    have been the whole set and every other test here would still be green.
    """
    h = Harness(events=_two_meetings())
    change = _pin(h)
    change.reply_template = ""
    change.reply_fragments = [{"source": "venue", "text": FOREIGN, "event_id": "evt-standup"}]
    cs = _approved(h, body=FOREIGN)

    outcome = h.executor.execute(cs.id)

    assert h.mail.sent == [], "the exemption excused a sentence the correspondent never wrote"
    assert "evt-standup" in (_email_effect(outcome).detail or "")

    # And the exemption is really doing something: the false positive it exists to stop is the one
    # the round found, and it is worth pinning here because a widened set is only justified by a
    # case it fixes. Two meetings share a place (`Old Cafe, King's Cross`) and the thread is named
    # after it, so once the pinned meeting has MOVED, `cafe` belongs to the second meeting and to
    # the correspondent's own subject — and nothing else.
    other = Harness(events=_two_meetings())
    about = other.mail.get_thread("t1")[0]
    day = other.calendar.list_events(f"{SAMPLE_DATE}T00:00:00+01:00",
                                     f"{SAMPLE_DATE}T23:59:59+01:00")
    other.calendar.update_event("ev1", other.calendar.get_event("ev1").etag,
                                start=at("15:00"), end=at("15:45"),
                                location="The Standard, King's Cross", send_updates="none")

    without = foreign_fragments_of(other.calendar.list_events(
        f"{SAMPLE_DATE}T00:00:00+01:00", f"{SAMPLE_DATE}T23:59:59+01:00"), pinned="ev1")
    with_exempt = foreign_fragments_of(
        other.calendar.list_events(f"{SAMPLE_DATE}T00:00:00+01:00",
                                   f"{SAMPLE_DATE}T23:59:59+01:00"),
        pinned="ev1", exempt_text=about.subject)

    assert "cafe" in about.subject.lower(), "the fixture does not exercise the shared word"
    assert any(fragment == "cafe" for _id, _field, fragment in without), \
        "the second meeting's place is not in the foreign set, so nothing was narrowed"
    assert not any(fragment == "cafe" for _id, _field, fragment in with_exempt), \
        "the correspondent's own subject did not exempt the word it carries"
    # The narrowing is by CONTENT and not by wholesale: the second meeting's clock is still there.
    assert any(fragment == "09:15" for _id, _field, fragment in with_exempt)
    assert day, "the fixture's day came back empty"


# ==================================================== finding 2 — the day and the window ---------
def test_finding_02_a_caller_supplied_offset_cannot_empty_the_window():
    """A `-12:00` stamp used to move the queried window twelve hours and empty the foreign set.

    The reviewed `send_reply` took the day off the change record and the OFFSET off
    `change.proposal["start"]` — a value a bundle carries and can therefore choose. A bundle whose
    slot said `-12:00` asked about a window the owner's day does not occupy, the day's other
    meetings fell outside it, the foreign set came back empty, and `guard_foreign_events` returned
    on `not foreign` — so the reply went out while the check reported nothing to report.

    The mutant restores that line (the offset read off the proposal). The window moves, the foreign
    set empties, and this test fails on the first assertion.
    """
    h = Harness(events=_two_meetings())
    change = _pin(h)
    # The slot the bundle carries, twelve hours away from the day the OWNER is in. The pinned event
    # still says 2026-09-19 and the owner's zone is still +01:00, so the window is unchanged — which
    # is the whole point: the bundle does not get to choose it.
    change.proposal = {"start": "2026-09-19T15:00:00-12:00"}
    change.reply_template = ""
    change.reply_fragments = [{"source": "venue", "text": FOREIGN, "event_id": "evt-standup"}]
    cs = _approved(h, body=FOREIGN)

    outcome = h.executor.execute(cs.id)

    assert h.mail.sent == [], "a caller-supplied offset emptied the foreign set and the reply left"
    assert "evt-standup" in (_email_effect(outcome).detail or "")


def test_finding_02_a_caller_supplied_day_does_not_move_the_window():
    """`care_day` was the same hole from the other side: the bundle named the day it was checked in.

    The reviewed line was `day = change.plan_day or args.get("care_day")`. The test below removes the
    plan day and supplies a day twelve hours away, with the pin left in place so the refusal is not
    the "no pin" one: the correct build takes the day off the pinned EVENT and still sees the second
    meeting, and the mutant takes it off the bundle, asks about a day nobody is in, and sends.

    This is the control that makes the removal exclusive. `test_finding_02_a_caller_supplied_offset
    _cannot_empty_the_window` removes the OFFSET half; this removes the DAY half; a mutation that
    restores either one alone turns exactly one of them red.
    """
    h = Harness(events=_two_meetings())
    change = _pin(h)
    change.plan_day = ""                             # the pin stays; the day the bundle can name goes
    change.reply_template = ""
    change.reply_fragments = [{"source": "venue", "text": FOREIGN, "event_id": "evt-standup"}]
    cs = _approved(h, body=FOREIGN, care_day="2026-09-18")

    outcome = h.executor.execute(cs.id)

    assert h.mail.sent == [], "the bundle named the day its own reply was checked in"
    assert "evt-standup" in (_email_effect(outcome).detail or "")
    # And the day really was the pinned event's: the reply is about the 19th, and the second
    # meeting is on the 19th. A build that asked about the 18th had nothing to compare against.
    assert "09:15" not in (_email_effect(outcome).detail or ""), \
        "the refusal quoted the text that revealed it"


def test_finding_02_a_caller_supplied_offset_cannot_empty_the_calendar_port_s_window():
    """The SAME line at the other port, which is where the review's own sentence puts it.

    Finding 2 is stated as "a foreign location and reply went out", and the reply half was the one
    the reviewer demonstrated. The calendar half was the same expression —
    `_day_window(slot[:10], slot)`, the day and the offset both read off the `start` the bundle is
    writing — and it guarded `location`, the field a model used to author end to end.

    The bundle below stamps the write twelve hours away from the owner's day and names the place of
    the day's OTHER meeting. Under the reviewed derivation the window moved with the stamp, that
    meeting fell outside it, the foreign set came back empty, and `guard_calendar_foreign` returned
    on `not foreign` — so the invite was written with another meeting's address and
    `send_updates="all"` notified its guests. Under this build the window is the PINNED event's day
    at the owner's zone, the meeting is inside it, and the write is refused.

    The mutant this is written against puts the caller's stamp back as the offset's source
    (`R6-5`), which is the reviewed line, and this test fails on the first assertion.

    The place is written from a catalogue record THIS CHANGE CHOSE, and that record renders the
    other meeting's own address. That is deliberate, and it is what keeps this test about the
    window: since round seven a `location` with no venue record behind it is refused by the venue
    check, which would fire first and hide whether the foreign check ever saw the day at all. With
    a real record behind it, the venue check passes and the FOREIGN check is the only one left.
    """
    from privatemesh.execute import FAILED
    from privatemesh.ports import CalendarEvent

    second = CalendarEvent(id="ev2", etag="etag-ev2", summary="Someone else", start=at("11:15"),
                           end=at("11:45"), timezone="Europe/London", location="Room 2, Town Hall",
                           attendees=("other@example.com",))
    h = Harness(events=[invite(), second])
    change = h.change_resolver.registered["c1"]
    change.venue_id = "v-other"
    change.venue_catalogue = [{"id": "v-other", "name": "Room 2", "address": "Town Hall",
                               "url": "https://example.com/room-2",
                               "title": "Room 2, Town Hall",
                               "excerpt": "The other meeting's address."}]
    cs = h.approved_changeset(calendar_args=h.calendar_args(
        start="2026-09-19T15:00:00-12:00", end="2026-09-19T15:45:00-12:00",
        location="Room 2, Town Hall"))

    outcome = h.executor.execute(cs.id)

    assert h.rec.surfaces("calendar") == [], \
        "a caller-supplied offset emptied the calendar port's window and the place was written"
    assert outcome.effects[0].state == FAILED
    assert "Room 2, Town Hall" not in (outcome.effects[0].detail or ""), \
        "the refusal quotes the value that was refused"
    assert h.calendar.get_event("ev1").location == "Old Cafe, King's Cross"

    # THE CONTROL: the same port, the same day's second meeting, a location that shares nothing with
    # it. It is written, so this is not a port that refuses every calendar write.
    clean = Harness(events=[invite(), second])
    ok = clean.approved_changeset(calendar_args=clean.calendar_args())
    clean.executor.execute(ok.id)
    assert [c["payload"]["location"] for c in clean.rec.surfaces("calendar")] \
        == ["The Standard, King's Cross"], "the calendar port refused an ordinary write"


# ================================================ finding 3 — the calendar's location ------------
def test_finding_03_a_venue_id_with_no_catalogue_entry_is_refused():
    """The model SELECTS; it does not author. An index the research never returned is a refusal.

    The reviewed `_venue` read `name` and `address` out of the model's answer and wrote them to the
    invite, so `location` was model text with a calendar write behind it. Now the engine builds a
    catalogue from what it actually fetched — each record with an id and its source — the model
    answers with an id, and the field is rendered from that record. An id with no entry is refused
    rather than quietly demoted to "no venue", because the second is how a model that invented a
    place would have its answer accepted as an absence.

    The mutant restores the reviewed `_venue`: the model's `name`/`address` become the venue again,
    which no longer raises, and this test fails at `pytest.raises`.
    """
    h = harness(search={}, script={"choose": {"venue_index": 0, "is_venue": True,
                                              "name": "The Ritz", "address": "150 Piccadilly",
                                              "reason_sentence": "It is open.", "rejected": []}})
    change = h.engine.detect(None)[0]

    with pytest.raises(VenueUnknown):
        h.engine.prepare(change.id)


def test_finding_03_a_venue_is_not_refused_by_the_name_a_model_wanted_to_give_it():
    """The control for the test above, and the reason the rule is "select" rather than "validate".

    Same engine, same script, one difference: the research FOUND something for the scripted index.
    The venue that reaches the proposal is the fetched page's, and the `name` the model wrote in the
    same answer is nowhere — which is what makes the refusal above a statement about provenance
    rather than about a model that happened to answer badly.
    """
    h = harness(script={"choose": {"venue_index": 0, "is_venue": True,
                                   "name": "The Ritz", "address": "150 Piccadilly",
                                   "reason_sentence": "It is open.", "rejected": []}})
    change = h.engine.prepare(h.engine.detect(None)[0].id)

    venue = change.proposal["venue"]
    assert venue["name"] == "The Standard, King's Cross"
    assert venue["venue_id"] == "venue-0"
    assert venue["evidence"]["url"].startswith("https://thestandard.co.uk")
    assert "Ritz" not in json.dumps(change.proposal), \
        "a place the model authored reached the proposal beside the one the engine fetched"


def test_finding_03_the_location_written_to_the_calendar_is_the_catalogue_s_render():
    """End to end, at the port: `location` is `render_venue(record)`, not `args` checked afterwards.

    The mutant this is written against is `_venue_not_rendered` answering "" — the reviewed build,
    which had no provenance question at the port at all. The bundle below is a real prepared change
    with a real catalogue, and the only thing wrong with it is the place it carries: this test then
    fails on the calendar half being `done` instead of refused.
    """
    h = harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.venue_id, "the prepared change carries no venue selection"
    assert change.venue_catalogue, "the prepared change carries no catalogue"
    # The changeset is built with the ids the FIXTURE's thread already uses, so the prepared change
    # is registered under them: the port reads the change by the id the bundle carries, and a bundle
    # pointing at a change the engine never prepared would be a different test.
    h.change_resolver.registered["c1"] = change

    cal_args = h.calendar_args(location="Room 2, Town Hall")
    cal_args["change_id"] = "c1"
    replied = " ".join([change.reply_template] + [f["text"] for f in change.reply_fragments])
    mail_args = h.email_args(body=replied)
    mail_args["change_id"] = "c1"
    cs = h.approved_changeset(calendar_args=cal_args, email_args=mail_args)

    outcome = h.executor.execute(cs.id)

    assert h.rec.surfaces("calendar") == [], "a place the research never returned was written"
    assert outcome.effects[0].state != "done", "the calendar write went through"
    assert CalendarRefused.__name__ in (outcome.effects[0].detail or "")
    assert "Room 2, Town Hall" not in (outcome.effects[0].detail or "")


# ======================================================= finding 4 — the digest ---------------
def test_finding_04_a_digest_line_whose_record_is_unknown_is_refused():
    """`record_id` was demanded by `DigestLine` and then dropped by `compose_digest`.

    The mutant removes the `known` check, which is exactly the reviewed build: any sentence at all
    could be labelled `care_coming`, and the fragment it rendered carried an EMPTY event id — so the
    body-equality check downstream had nothing to compare against and an unrendered line passed.
    """
    lines = [DigestLine(source="care_coming", record_id="does-not-exist", text="Standup at 09:15")]

    # Stated as an assertion rather than left to `pytest.raises`, because the mutant of this line
    # does not only remove the refusal — it makes the NEXT line index a mapping that has no such
    # key. Without the `except` the test still goes red, but on a `KeyError` escaping the call under
    # test: red without an assertion about what went wrong, which is evidence the guard is reached
    # and no evidence at all about the sentence this test claims. See `redteam/mutcheck_findings.py`,
    # which counts only the first.
    try:
        compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",), coming=lines,
                       records={"ev1": "Standup at 09:15"})
    except DigestBadLine:
        return
    except Exception as exc:                                         # noqa: BLE001
        pytest.fail(f"a record this build cannot read was refused with {type(exc).__name__} "
                    f"rather than a DigestBadLine: {exc}")
    pytest.fail("a digest line naming a record this build cannot read was composed")


def test_finding_04_the_record_id_travels_onto_the_fragment():
    """The other half: required is not enough, it has to be CARRIED.

    A digest fragment with an empty `event_id` is a fragment whose provenance is a string somebody
    typed. This is the control for the refusal above — same call, a record that exists — and it
    asserts the id on the fragment, which is the field the mutant of finding 4 leaves empty.
    """
    plan = compose_digest(
        day=SAMPLE_DATE, recipients=("alex@example.com",),
        coming=[DigestLine(source="care_coming", record_id="ev1", text="Standup at 09:15")],
        records={"ev1": "Standup at 09:15"})

    carried = [f.event_id for f in plan.fragments if f.source == "care_coming"]
    assert carried == ["ev1"], f"the record id did not travel onto the fragment: {carried}"


def test_finding_04_a_digest_whose_records_were_never_offered_still_refuses_a_bare_string():
    """An empty record set means "this caller can point at nothing", not "anything goes".

    The record set is the second lock on the same door; the first is that a line is a `DigestLine`
    at all. A caller with no records does not get to pass a string, and this test is the one that
    fails if `DigestBadLine` is ever relaxed to accept one. There is no `records=None` any more —
    the SEVENTH review's fourth finding is that "the caller offered no record set" was itself a way
    through, because a line's own id was then all there was to go on — so the state this test can
    reach is the empty mapping, which is the same claim with nothing to hide behind.
    """
    with pytest.raises(DigestBadLine):
        compose_digest(day=SAMPLE_DATE, recipients=("alex@example.com",),
                       coming=["Standup at 09:15"], records={})


# ============================================= the venue catalogue itself ------------------------
def test_the_catalogue_is_built_from_what_was_fetched_and_nothing_else():
    """One reader, and it only ever reads fetched bytes.

    The place on the calendar is now composed rather than scanned, so the catalogue is where the
    composition gets its words. A catalogue entry with no source is a place with no provenance.
    """
    venues = [{"url": "https://thestandard.co.uk/", "title": "The Standard, King's Cross",
               "excerpt": "42 Euston Road. Open daily 12:00-23:00", "official": True}]
    catalogue = venue_catalogue(venues)

    assert [r["id"] for r in catalogue] == ["venue-0", "venue-none"]
    assert catalogue[0]["source_url"] == "https://thestandard.co.uk/"
    assert catalogue[0]["official"] is True
    assert catalogue[0]["address"] == "42 Euston Road"
    assert catalogue[-1]["name"] == "Venue to confirm"
    # Deterministic, and it is a READER of fetched bytes rather than a generator of prose: a page
    # with no address in it yields no address, which is why an unfound street cannot be invented.
    assert venue_address_in("Open daily 12:00-23:00") == ""
