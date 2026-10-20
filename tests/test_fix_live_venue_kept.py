"""The 2026-09-22 live caregiver run: two bugs in one shape.

An email asks to move "Benefits assessment (Rosa)" from 13:00 to 13:45 — a pure time change, on a
meeting that already has a place. What actually happened: venue research ran anyway, searched for a
café near nothing in particular, found nothing, and the calendar diff would have replaced
"Northgate Clinic, Room 4" with "Venue to confirm" — and the reply the owner would have approved
read "Hi Alex, Alex Doe Action needed: confirm Rosa's benefits record 13:45-14:30 works for me.
Venue to confirm. Owner", because the draft picked the recipient's name, the thread's subject and
the literal placeholder owner name as body fragments and the composer dutifully rendered all of it.

Both are reproduced end to end through the sample/fake providers, with a draft script that picks
exactly the fragments the live draft picked — this is not a hypothetical model, it is the one that
ran."""
from __future__ import annotations

from privatemesh import changes as ch
from privatemesh.ports import CalendarEvent

from fakes_v2 import Harness, at, change_email, evidence

LOCATION = "Northgate Clinic, Room 4"
#: The corpus's placeholder correspondent (`fakes_v2.change_email`'s own default) standing in for
#: the live run's sender. That sender is a real person — the run was driven from a real inbox — so
#: their name and address were replaced with this fictional pair before the repository went public.
#: The string the fixtures and the draft see is the placeholder; what the tests measure, which is
#: the SHAPE of the live event, thread and bad draft, is unchanged.
SENDER_ADDRESS = "alex@example.com"
SENDER = f"Alex Doe <{SENDER_ADDRESS}>"


def _event(location: str = LOCATION) -> CalendarEvent:
    return CalendarEvent(id="ev1", etag="etag-1", summary="Benefits assessment (Rosa)",
                         start=at("13:00"), end=at("13:45"), timezone="Europe/London",
                         location=location, attendees=(SENDER_ADDRESS,))


def _thread_message():
    return change_email(
        "m1", sender=SENDER, subject="Action needed: confirm Rosa's benefits record",
        body="Hi, can we move the benefits assessment from 13:00 to 13:45 on Tuesday? "
             "Everything else stays the same. Thanks, Alex")


#: The exact fragments the live draft picked, reproduced. `time` and `venue` are the words a
#: catalogue offers for THIS change (see the assertions below for why they must match verbatim to
#: be eligible); `recipient`, `thread_subject` and `owner_name` are the three that garbled the live
#: reply, offered here exactly as the live draft offered them.
LIVE_DRAFT = {
    "template_body": "Hi Alex,",
    "fragments": [
        {"source": "recipient", "text": "Alex Doe"},
        {"source": "thread_subject", "text": "Action needed: confirm Rosa's benefits record"},
        {"source": "time", "text": "13:45-14:30 works for me."},
        {"source": "venue", "text": f"{LOCATION}."},
        {"source": "owner_name", "text": "Owner"},
    ],
}


def _harness(**over):
    script = {
        "classify": {"is_meeting_change": True, "reason": "asks to move the appointment"},
        "extract": {"options": [{"start": at("13:45"), "end": at("14:30"), "phrase": "13:45"}],
                    "request_excerpt": "Move the benefits assessment to 13:45."},
        "draft": LIVE_DRAFT,
    }
    script.update(over.pop("script", {}))
    return Harness(messages=[_thread_message()], threads={"t1": [_thread_message()]},
                   events=[over.pop("event", _event())], search={}, script=script, **over)


def test_a_time_only_move_of_an_event_with_a_place_never_searches():
    """The High: venue research must not run at all when the meeting already has a place and
    nothing asks to change it — not "runs and comes back empty", never runs."""
    h = _harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    assert h.search.queries == [], f"a search ran when none should have: {h.search.queries}"
    assert "query" not in h.tf.phases(), "the query phase ran with no search to plan"
    assert "choose" not in h.tf.phases(), "the choose phase ran with nothing fetched to choose from"


def test_a_time_only_move_never_writes_venue_to_confirm_over_a_real_place():
    """The heart of the High: the field that travels to the calendar must be the place that was
    already there, never the placeholder — under any path, including this one."""
    h = _harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    cs = h.changesets.for_change(change.id)
    assert cs.calendar.arguments["location"] == LOCATION
    diff = cs.calendar.display["diff"]
    # The diff shows no location CHANGE: old and new are the one real place, never the placeholder
    # and never a location the owner did not already have on this meeting.
    assert diff["location"] == [LOCATION, LOCATION], diff["location"]
    assert "Venue to confirm" not in str(diff["location"])
    assert change.proposal["venue"]["name"] == LOCATION
    assert change.proposal["venue"]["evidence"] is None, "nothing was researched to be evidence for"


def test_the_researched_cafe_flow_still_searches_when_the_request_asks_for_a_place():
    """The control: a request that DOES ask for a place — the existing café hero flow's own words,
    "near King's Cross ... You choose" — still researches. The rule is about the request, not a
    blanket switch that turns venue research off."""
    from test_changes import TIMES, harness as cafe_harness

    h = cafe_harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    assert h.search.queries, "the café flow's own request stopped searching"
    assert change.proposal["venue"]["name"] == "The Standard, King's Cross"


def test_the_live_drafts_bad_picks_no_longer_garble_the_reply():
    """The Medium: even fed the EXACT fragments the live draft chose — the recipient's full name,
    the thread's subject line, and the literal "Owner" placeholder — the composed reply is clean,
    because none of those three is in the catalogue any more; only what the engine actually
    rendered for this change survives into the body."""
    h = _harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    cs = h.changesets.for_change(change.id)
    body = cs.email.arguments["body"]

    assert body.startswith("Hi Alex,"), body
    assert "13:45-14:30 works for me." in body
    assert LOCATION in body
    assert "Venue to confirm" not in body
    # The three fragments the live draft picked that do not belong in a body: the correspondent's
    # full name a second time (the greeting already carries it), the thread's own subject line
    # read out as a sentence, and the unconfigured-owner placeholder.
    assert "Alex Doe" not in body, body
    assert "Action needed" not in body, body
    assert "Owner" not in body, body
    # The composer dropped them rather than refusing the change outright — the move is not
    # cancelled by a draft that reached past what it was offered.
    assert body == "Hi Alex, 13:45-14:30 works for me. Northgate Clinic, Room 4."


def test_a_meeting_with_no_place_yet_still_researches_one():
    """The other control: this rule is about a place that already EXISTS, not about skipping
    research whenever the model is not asked for a venue explicitly. An event with an empty
    location still gets researched, same as before this fix."""
    h = _harness(event=_event(location=""),
                 script={"query": [{"query": "cafe near Northgate"}],
                        "choose": {"venue_index": None, "is_venue": False,
                                   "reason_sentence": "No pages came back.", "rejected": []}})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    assert h.search.queries, "an event with no place at all should still be researched"


# ================================================================================================
# Round 2 — the review's finding: a GENUINE venue request whose research finds nothing.
#
# The round-1 fix asked "did the sender ask for a place?" and, when the answer was no, never
# searched. This is the other half of the same invariant, and the one the review found still open:
# the sender DOES ask ("let's do a café instead"), the research DOES run, it comes back with nothing
# usable, and the plan wrote "Venue to confirm" over "Northgate Clinic, Room 4". The rule is not
# about the request at all — a marker that states no place may never overwrite a place the meeting
# already has — and it is enforced in `changes.venue_for_write`, at the point the calendar write's
# venue and location are decided, on both the prepare path and the owner-edit path.
# ================================================================================================

#: The same live event and the same sender, with the one word changed that makes this a venue
#: request: the caregiver asks for a café instead of the clinic.
CAFE_REQUEST = ("Hi, let's do a café instead. Can we move the benefits assessment from 13:00 to "
                "13:45 on Tuesday? Everything else stays the same. Thanks, Alex")

#: What the research comes back with: pages about the place, none of them a venue with hours — the
#: live run's own results, which is why the choose phase can only answer "none of these".
NOT_A_VENUE = [
    evidence("https://tfl.gov.uk/northgate-station-map", "Northgate station map",
             "Plan your journey through Northgate. Lifts and escalators.", official=False),
    evidence("https://networkrail.co.uk/northgate-works", "Northgate upgrade engineering notice",
             "Weekend engineering work closes platforms 1 to 3. Replacement buses run.",
             official=False),
]


def _cafe_request_message():
    return change_email("m1", sender=SENDER,
                        subject="Action needed: confirm Rosa's benefits record",
                        body=CAFE_REQUEST)


def _finding_harness(**over):
    """The finding's exact scenario: a place already on the meeting, a sender who asks for a new
    one, and research that returns nothing usable. `script` overrides carry the controls."""
    script = {
        "classify": {"is_meeting_change": True, "reason": "asks to move the appointment"},
        "extract": {"options": [{"start": at("13:45"), "end": at("14:30"), "phrase": "13:45"}],
                    "request_excerpt": "Let's do a café instead, and move it to 13:45."},
        "query": [{"query": "cafes near Northgate open Tuesday opening hours"}],
        "choose": {"venue_index": 0, "is_venue": False,
                   "reason_sentence": "The nearest result is a station map, not a venue.",
                   "rejected": []},
        "draft": LIVE_DRAFT,
    }
    script.update(over.pop("script", {}))
    message = _cafe_request_message()
    return Harness(messages=[message], threads={"t1": [message]},
                   events=[over.pop("event", _event())],
                   search=over.pop("search", {"northgate": NOT_A_VENUE}),
                   script=script, **over)


def test_the_finding_a_genuine_cafe_request_that_finds_nothing_keeps_the_real_place():
    """The re-review's finding, end to end through the sample/fake providers.

    All three conditions at once: the event carries "Northgate Clinic, Room 4", the sender asks for
    a café instead, and the research returns no usable venue. The place that travels to the
    calendar — and the diff the owner approves it from — is the one the meeting already had."""
    h = _finding_harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    # The request DID ask for a place, so the research ran: this is not the round-1 early exit
    # being measured a second time.
    assert h.search.queries, "a request that asks for a café must still be researched"
    assert "choose" in h.tf.phases(), "the choose phase is what answers 'is this a venue?'"

    cs = h.changesets.for_change(change.id)
    assert cs.calendar.arguments["location"] == LOCATION
    assert cs.calendar.display["diff"]["location"] == [LOCATION, LOCATION], \
        cs.calendar.display["diff"]
    assert "Venue to confirm" not in str(cs.calendar.arguments)
    assert "Venue to confirm" not in str(cs.calendar.display)

    venue = change.proposal["venue"]
    assert venue["name"] == LOCATION
    assert venue["evidence"] is None, "nothing was researched to be evidence for anything"
    assert venue["venue_id"] == ch.VENUE_KEPT_ID
    assert "no new venue was found" in venue["note"].lower(), venue["note"]
    # The place was KEPT, not blanked: an empty location would be the same absence wearing a
    # different spelling.
    assert LOCATION in cs.email.arguments["body"]
    assert "Venue to confirm" not in cs.email.arguments["body"]


def test_the_finding_the_write_the_port_will_check_is_the_one_that_was_approved():
    """The guard is only worth anything if it holds at the port as well as in the proposal.

    `execute._venue_not_rendered` refuses a `location` that is not the render of the record the
    change's `venue_id` names. A guard that kept the place but left the catalogue without the entry
    that names it would therefore turn this write into a refusal — safe, and not the fix. This is
    the same question the port asks, asked here."""
    from privatemesh.changes import render_venue, venue_record
    from privatemesh.execute import _venue_not_rendered

    h = _finding_harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    cs = h.changesets.for_change(change.id)
    location = cs.calendar.arguments["location"]

    record = venue_record(change.venue_catalogue, change.venue_id)
    assert record is not None, "the catalogue the change stores has no entry its venue_id names"
    assert render_venue(record) == location
    assert _venue_not_rendered(change, location) == ""


def test_a_found_venue_in_the_same_scenario_still_moves_the_meeting():
    """The control, and the reason the guard is a rule about placeholders rather than a switch that
    freezes the location: the SAME event, the SAME café request, the SAME research — with one
    result that is a real café. The invite moves to it. A guard that kept the old place here would
    be a product that cannot accept the change the owner's correspondent asked for."""
    cafe = evidence("https://fernandfinch.example/", "Fern & Finch Café",
                    "12 York Way. Open daily 08:00-18:00. Kitchen closes at 17:00.", official=True)
    # The café is the FIRST result and the script points at index 0, so what the plan picks is
    # unambiguous: the change is measured, not the model's taste in indices.
    h = _finding_harness(search={"northgate": [cafe, *NOT_A_VENUE]},
                         script={"choose": {"venue_index": 0, "is_venue": True,
                                            "reason_sentence": "Its own page gives the hours.",
                                            "rejected": []}})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    cs = h.changesets.for_change(change.id)
    assert change.proposal["venue"]["name"] == "Fern & Finch Café"
    assert change.proposal["venue"]["venue_id"] == "venue-0"
    assert cs.calendar.arguments["location"] == "Fern & Finch Café, 12 York Way"
    assert cs.calendar.display["diff"]["location"] == [
        LOCATION, "Fern & Finch Café, 12 York Way"], cs.calendar.display["diff"]
    assert "Venue to confirm" not in str(cs.calendar.display)


def test_an_edit_cannot_carry_the_placeholder_forward_from_a_bundle_that_holds_it():
    """The guard is on the EDIT path as well as the prepare path, and this is the case that makes
    that more than tidiness: a change prepared by the build this finding was made against is still
    on disk, and its bundle still holds "Venue to confirm" over the meeting's real place. An owner
    editing that change — moving the time, say — must not carry the placeholder into the version
    they approve.

    The legacy state is built the way that build left it: the proposal's venue is the engine's own
    "no venue" record, and the change set's calendar arguments carry its render. Nothing else is
    touched — the owner's edit below is an ordinary time change."""
    from privatemesh.changes import VENUE_NONE_ID, VENUE_NONE_NAME
    from privatemesh.execute import CALENDAR_SKILL, EMAIL_SKILL

    from test_changes import harness as cafe_harness

    h = cafe_harness()
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    cs = h.changesets.for_change(change.id)
    existing = h.calendar.get_event(cs.calendar.arguments["event_id"]).location
    assert existing, "this control needs an event that already has a place"

    legacy = {**cs.calendar.arguments, "location": VENUE_NONE_NAME}
    cal = h.approvals.request(change.id, CALENDAR_SKILL, legacy)
    mail = h.approvals.request(change.id, EMAIL_SKILL, dict(cs.email.arguments))
    legacy_cs = h.changesets.edit(cs.id, approval_ids=(cal.id, mail.id), calendar_args=legacy)
    change = h.engine.require(change.id)
    change.proposal["venue"] = {"name": VENUE_NONE_NAME, "address": "", "evidence": None,
                                "note": "Opening hours not verified.", "rejected": [],
                                "venue_id": VENUE_NONE_ID}
    h.engine._persist(change)

    revised = h.engine.revise(change.id, start=at("14:00"))
    assert revised.stage == "proposal_ready", revised.error
    new_cs = h.changesets.for_change(change.id)
    assert new_cs.version == legacy_cs.version + 1, "the owner's edit did not mint the next version"
    assert new_cs.calendar.arguments["location"] == existing, \
        new_cs.calendar.arguments["location"]
    assert new_cs.calendar.display["diff"]["location"] == [existing, existing], \
        new_cs.calendar.display["diff"]
    assert revised.proposal["venue"]["name"] == existing
    assert revised.proposal["venue"]["venue_id"] == ch.VENUE_KEPT_ID
    assert "Venue to confirm" not in str(new_cs.calendar.arguments)
    assert "Venue to confirm" not in str(new_cs.calendar.display)


def test_the_builder_never_writes_a_placeholder_over_a_real_location():
    """The unit test on the builder itself, so the invariant is measured where it is enforced and
    not only through the flow that happens to exercise it.

    Every spelling of "no place" — the engine's own placeholder record, and a fetched record whose
    page gives neither a name nor an address — against a meeting that already has a place. Both are
    in the catalogue the write is rendered from, because a guard that only recognised the first
    would be walked past by the second."""
    station = {"title": "Northgate station map", "excerpt": "Lifts and escalators.",
               "url": "https://tfl.gov.uk/northgate", "official": False}
    rejected = [{"name": "Northgate station map", "reason": "not a venue with opening hours"}]
    cases = [
        # The engine's "no venue was confirmed" record, which is what a choose phase answering
        # `is_venue: false` selects.
        (ch.venue_catalogue([station]),
         {"name": ch.VENUE_NONE_NAME, "address": "", "evidence": None,
          "note": "Opening hours not verified — the results were not venues with opening hours.",
          "rejected": rejected, "venue_id": ch.VENUE_NONE_ID}),
        # The other spelling: a result the engine did fetch and could render no place out of.
        (ch.venue_catalogue([{"title": "", "excerpt": "", "url": "https://example.invalid/x",
                              "official": False}]),
         {"name": "", "address": "", "evidence": None, "note": "Opening hours not verified.",
          "rejected": [], "venue_id": "venue-0"}),
    ]
    for catalogue, placeholder in cases:
        kept, location = ch.venue_for_write(LOCATION, catalogue, placeholder)
        assert location == LOCATION, (placeholder["venue_id"], location)
        assert kept["venue_id"] == ch.VENUE_KEPT_ID
        assert kept["name"] == LOCATION
        assert kept["evidence"] is None
        # The search's own rejections survive, so the owner still reads why nothing was chosen.
        assert kept["rejected"] == placeholder["rejected"]
        # The catalogue the change will store has to carry the record the port re-derives from.
        stored = ch.with_kept_venue(catalogue, kept)
        record = ch.venue_record(stored, kept["venue_id"])
        assert record is not None and ch.render_venue(record) == LOCATION


def test_the_builder_still_states_no_place_when_the_meeting_has_none():
    """The other side of the invariant, and the reason it is a guard and not a blanket rule: a
    meeting with NO place has nothing to protect, so "Venue to confirm" is still the honest answer
    and is still what is written. A guard that refused or invented a place here would be a product
    that cannot move a meeting into a place nobody has confirmed."""
    catalogue = ch.venue_catalogue([])
    placeholder = {"name": ch.VENUE_NONE_NAME, "address": "", "evidence": None,
                   "note": "Opening hours not verified.", "rejected": [],
                   "venue_id": ch.VENUE_NONE_ID}
    venue, location = ch.venue_for_write("", catalogue, placeholder)
    assert location == ch.VENUE_NONE_NAME
    assert venue["venue_id"] == ch.VENUE_NONE_ID
    assert venue["note"] == placeholder["note"], "the note was rewritten for a case that is not it"


def test_the_builder_leaves_a_researched_venue_alone():
    """The second control on the builder: a record that names a real place passes through
    untouched, with the record's own words and its own note."""
    fetched = [{"title": "Fern & Finch Café", "excerpt": "12 York Way. Open daily 08:00-18:00.",
                "url": "https://fernandfinch.example/", "official": True}]
    catalogue = ch.venue_catalogue(fetched)
    record = ch.venue_record(catalogue, "venue-0")
    venue = {"name": record["name"], "address": record["address"], "evidence": fetched[0],
             "note": "Opening hours checked; seating not reserved.", "rejected": [],
             "venue_id": "venue-0"}
    kept, location = ch.venue_for_write(LOCATION, catalogue, venue)
    assert kept == venue
    assert location == "Fern & Finch Café, 12 York Way"
