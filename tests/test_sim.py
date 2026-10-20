"""sim: the sample-mode ports. This is what a judge touches first, so it has to be the same shape
as the live connectors, deterministic, and honest about being synthetic.

The last test in this file is the important one: the simulator is only a fair preview of the real
engine if it satisfies the same protocols the live connectors do.
"""
from __future__ import annotations

import inspect

import pytest

from privatemesh import sim
from privatemesh.ports import CalendarPort, EtagMismatch, MailPort, SearchPort


@pytest.fixture
def ports():
    return sim.build_sample_ports()


# ------------------------------------------------------------------ labelled sample ------------
def test_every_simulator_declares_itself_sample_data(ports):
    assert ports.mail.is_sample and ports.calendar.is_sample and ports.search.is_sample
    assert "sample" in sim.SAMPLE_NOTICE.lower()
    assert "no provider" in sim.SAMPLE_NOTICE.lower() or "synthetic" in sim.SAMPLE_NOTICE.lower()
    # What `GET /v2/session` reports for sample mode: every service connected, every detail labelled.
    connections = ports.connections()
    assert [c["service"] for c in connections] == ["gmail", "calendar", "tavily"]
    assert all(c["state"] == "connected" and "sample" in c["detail"].lower()
               for c in connections)


def test_evidence_is_labelled_wherever_a_synthetic_fact_could_be_mistaken_for_a_checked_one(ports):
    """The label is in the excerpt, not the title: the engine takes a venue's NAME from the title,
    so a marker there would be the venue's name on the review screen and in the reply."""
    found = ports.search.search("Fern & Finch Café King's Cross opening hours")
    assert found
    assert all(e.excerpt.startswith(sim.SAMPLE_PREFIX) for e in found)
    assert not any(e.title.startswith(sim.SAMPLE_PREFIX) for e in found)


def test_the_sample_is_deterministic_across_runs():
    first, second = sim.build_sample_ports(), sim.build_sample_ports()
    assert first.mail.list_new(None)[0] == second.mail.list_new(None)[0]
    a, b = first.search.search("King's Cross venue"), second.search.search("King's Cross venue")
    assert [(x.url, x.excerpt, x.retrieved_at, x.official) for x in a] == \
           [(x.url, x.excerpt, x.retrieved_at, x.official) for x in b]
    assert first.calendar.list_events("2026-09-19T00:00:00+01:00",
                                      "2026-09-20T00:00:00+01:00") == \
           second.calendar.list_events("2026-09-19T00:00:00+01:00", "2026-09-20T00:00:00+01:00")


# ------------------------------------------------------------------ the inbox ------------------
def test_the_watched_inbox_carries_alexs_change_request(ports):
    messages, cursor = ports.mail.list_new(None)
    assert [m.id for m in messages] == ["msg-alex-cafe"]
    request = messages[0]
    assert "alex@example.com" in request.sender
    assert "closed" in request.subject.lower()
    assert "King's Cross" in request.body_text and "3 or 4" in request.body_text
    assert cursor


def test_an_unwatched_message_never_enters_the_change_flow(ports):
    everything = ports.mail.list_new(None)[0]
    assert "msg-invoice" not in [m.id for m in everything]


def test_the_sample_cursor_resumes_without_duplicates(ports):
    _, cursor = ports.mail.list_new(None)
    again, _ = ports.mail.list_new(cursor)
    assert again == []


def test_the_thread_can_be_read_back(ports):
    messages, _ = ports.mail.list_new(None)
    thread = ports.mail.get_thread(messages[0].thread_id)
    assert [m.id for m in thread] == ["msg-alex-cafe"]
    assert thread[0].rfc822_message_id


def test_a_sample_reply_is_threaded_and_idempotent(ports):
    original = ports.mail.list_new(None)[0][0]
    sent = ports.mail.send_reply(original, to=("Alex Doe <alex@example.com>",),
                                 subject="Re: Our usual café has closed",
                                 body_text="15:00 works — invite updated.",
                                 idempotency_key="sample-key-1")
    assert sent.thread_id == original.thread_id
    assert ports.mail.find_sent("sample-key-1") == sent
    again = ports.mail.send_reply(original, to=("Alex Doe <alex@example.com>",),
                                  subject="Re: Our usual café has closed",
                                  body_text="15:00 works — invite updated.",
                                  idempotency_key="sample-key-1")
    assert again == sent
    assert len(ports.mail.outbox) == 1, "the sample must not model a duplicate send"


# ------------------------------------------------------------------ the day --------------------
def test_the_day_holds_the_45_minute_meeting_owned_by_the_owner(ports):
    events = ports.calendar.list_events("2026-09-19T00:00:00+01:00", "2026-09-20T00:00:00+01:00")
    meeting = next(e for e in events if e.id == sim.SAMPLE_MEETING_ID)
    assert meeting.summary == "Catch up with Alex"
    assert meeting.start == "2026-09-19T11:00:00+01:00"
    assert meeting.end == "2026-09-19T11:45:00+01:00"
    assert meeting.organizer_is_self is True
    assert meeting.recurring is False
    assert "alex@example.com" in meeting.attendees


def test_the_protected_commitment_never_appears_as_a_calendar_event(ports):
    events = ports.calendar.list_events("2026-09-19T00:00:00+01:00", "2026-09-20T00:00:00+01:00")
    assert all("protected" not in (e.summary or "").lower() for e in events)
    protected = ports.calendar.protected_commitment()
    assert protected.start == "2026-09-19T16:30:00+01:00"
    assert protected.buffer_minutes == 45
    assert protected.version == 1


def test_the_scenario_arithmetic_holds_1500_fits_and_1600_does_not(ports):
    """The kernel decides this, not the simulator — but the sample data has to make it true."""
    assert ports.calendar.free_busy("2026-09-19T15:00:00+01:00", "2026-09-19T15:45:00+01:00") == []
    protected = ports.calendar.protected_commitment()
    # 45 minutes of meeting plus the owner's 45-minute buffer must end by 16:30 -> latest start
    # 15:00. A 16:00 start overruns the boundary.
    assert protected.start == "2026-09-19T16:30:00+01:00"
    assert protected.buffer_minutes == 45


def test_free_busy_reports_the_real_commitments_of_the_day(ports):
    busy = ports.calendar.free_busy("2026-09-19T00:00:00+01:00", "2026-09-20T00:00:00+01:00")
    assert [(b.start, b.end) for b in busy] == [
        ("2026-09-19T09:15:00+01:00", "2026-09-19T09:30:00+01:00"),
        ("2026-09-19T11:00:00+01:00", "2026-09-19T11:45:00+01:00"),
        ("2026-09-19T13:00:00+01:00", "2026-09-19T13:45:00+01:00"),   # somebody else's meeting
        ("2026-09-19T14:00:00+01:00", "2026-09-19T14:30:00+01:00"),
    ]


def test_the_sample_day_includes_an_event_the_owner_does_not_organise(ports):
    events = ports.calendar.list_events("2026-09-19T00:00:00+01:00", "2026-09-20T00:00:00+01:00")
    assert any(not e.organizer_is_self for e in events)


# ------------------------------------------------------------------ moving the meeting --------
def test_a_sample_update_is_conditional_and_reads_back(ports):
    before = ports.calendar.get_event(sim.SAMPLE_MEETING_ID)
    after = ports.calendar.update_event(
        sim.SAMPLE_MEETING_ID, before.etag, start="2026-09-19T15:00:00+01:00",
        end="2026-09-19T15:45:00+01:00", location="Fern & Finch Café, 18 Willow Street",
        send_updates="all")
    assert after.start == "2026-09-19T15:00:00+01:00"
    assert after.location == "Fern & Finch Café, 18 Willow Street"
    assert after.etag != before.etag, "a changed event is a new version"
    assert ports.calendar.get_event(sim.SAMPLE_MEETING_ID).start == "2026-09-19T15:00:00+01:00"


def test_a_stale_review_is_refused_by_the_sample_too(ports):
    stale = ports.calendar.get_event(sim.SAMPLE_MEETING_ID).etag
    ports.calendar.advance(sim.SAMPLE_MEETING_ID)          # the event moved while you decided
    with pytest.raises(EtagMismatch):
        ports.calendar.update_event(sim.SAMPLE_MEETING_ID, stale,
                                    start="2026-09-19T15:00:00+01:00",
                                    end="2026-09-19T15:45:00+01:00", location="x",
                                    send_updates="all")


def test_the_sample_refuses_what_the_live_connector_refuses(ports):
    events = ports.calendar.list_events("2026-09-19T00:00:00+01:00", "2026-09-20T00:00:00+01:00")
    not_mine = next(e for e in events if not e.organizer_is_self)
    with pytest.raises(sim.SimError) as err:
        ports.calendar.update_event(not_mine.id, not_mine.etag,
                                    start="2026-09-19T15:00:00+01:00",
                                    end="2026-09-19T15:45:00+01:00", location="x",
                                    send_updates="all")
    assert "organis" in str(err.value).lower()
    with pytest.raises(ValueError):
        ports.calendar.update_event(sim.SAMPLE_MEETING_ID, "", start="2026-09-19T15:00:00+01:00",
                                    end="2026-09-19T15:45:00+01:00", location="x",
                                    send_updates="all")


def test_the_sample_refuses_a_recurring_series_the_same_way():
    """Parity with the live connector, which refuses a series before it writes anything."""
    from privatemesh.ports import CalendarEvent

    series = sim.SimCalendar(events=[sim._SimEvent(
        CalendarEvent(id="evt-weekly", etag="sim-etag-weekly-1", summary="Weekly 1:1",
                      start="2026-09-19T10:00:00+01:00", end="2026-09-19T10:30:00+01:00",
                      timezone="Europe/London", recurring=True), recurring=True)])
    with pytest.raises(sim.SimError) as err:
        series.update_event("evt-weekly", "sim-etag-weekly-1",
                            start="2026-09-19T15:00:00+01:00",
                            end="2026-09-19T15:45:00+01:00", location="x", send_updates="all")
    assert "recurring" in str(err.value).lower()
    assert series.get_event("evt-weekly").start == "2026-09-19T10:00:00+01:00"


# ------------------------------------------------------------------ venues --------------------
def test_search_returns_the_selected_venue_with_its_supporting_excerpt(ports):
    """The venue the contract names, with a page whose own hours cover the proposed 15:00."""
    found = ports.search.search("Fern & Finch Café King's Cross opening hours",
                                subject="Fern & Finch Café")
    selected = next(e for e in found if "fernandfinch.co.uk" in e.url)
    assert selected.official is True
    assert "08:00-18:00" in selected.excerpt
    assert selected.retrieved_at.endswith("Z")


def test_search_returns_one_candidate_that_fails_the_time_check(ports):
    found = ports.search.search("King's Cross venue for a 15:00 meeting",
                                subject="Corner House Café")
    closed = [e for e in found if "closed at 15:00" in e.excerpt]
    assert closed, "the sample must carry a venue that did not survive the check, with its reason"


def test_the_rejected_venue_is_the_one_closed_at_the_proposed_time(ports):
    found = ports.search.search("venues near King's Cross", subject="Corner House Café")
    corner = next(e for e in found if "cornerhousecafe" in e.url)
    assert "closed" in corner.excerpt.lower()
    assert "15:00" in corner.excerpt


def test_an_aggregator_page_is_not_official_even_when_it_ranks(ports):
    found = ports.search.search("King's Cross cafés", subject="Fern & Finch Café")
    yelp = [e for e in found if "yelp" in e.url]
    assert yelp and yelp[0].official is False


def test_include_domains_and_max_results_are_honoured(ports):
    found = ports.search.search("King's Cross venue", include_domains=("fernandfinch.co.uk",))
    assert [e.url for e in found] == ["https://www.fernandfinch.co.uk/kings-cross"]
    assert len(ports.search.search("King's Cross venue", max_results=1)) == 1


def test_a_query_with_no_matching_venue_returns_no_evidence(ports):
    assert ports.search.search("a venue in Reykjavik") == []


# ------------------------------------------------------------------ protocol fit --------------
def test_all_three_simulators_satisfy_the_ports_the_engine_calls(ports):
    for port, impl in ((MailPort, ports.mail), (CalendarPort, ports.calendar),
                       (SearchPort, ports.search)):
        for name, member in vars(port).items():
            if name.startswith("_") or not callable(member):
                continue
            assert callable(getattr(impl, name, None)), f"{type(impl).__name__} has no {name}()"
            want = inspect.signature(member)
            got = inspect.signature(getattr(impl, name))
            for param, spec in want.parameters.items():
                if param == "self":
                    continue
                assert param in got.parameters, f"{type(impl).__name__}.{name} lacks {param!r}"
                assert got.parameters[param].kind == spec.kind, f"{name}.{param}"
