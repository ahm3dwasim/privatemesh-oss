"""The live run's venue research: station maps and engineering notices, hours unverified.

The real Tavily results for "near King's Cross" were mostly station maps, rail engineering notices
and station pages. The plan still came back naming a café whose opening hours nothing had checked,
because the choose phase would accept any index it was handed.

Two things were missing, and they are different things:

* the QUERY phase never asked for the kind of page that can answer the question — a café or a
  restaurant, with its opening hours, on the day the meeting is for;
* the CHOOSE phase had no way to say "none of these is a venue", and the engine had no way to
  refuse a choice that was one. `is_venue` is that answer, and it is REQUIRED: a missing one makes
  the engine act on a different answer than the model gave, which is the rule this codebase marks
  required keys by.

The two-round cap and the privacy gate on every query are properties of the flow and are asserted
here rather than rebuilt — this fix must not widen either.
"""
from __future__ import annotations

import pytest

from privatemesh import changes as ch
from privatemesh.boundary import GuardedSearch

from fakes_v2 import Harness, change_email, evidence

#: What the live search actually returned for "near King's Cross": pages about the station.
STATION_MAP = evidence("https://tfl.gov.uk/kings-cross-station-map", "King's Cross station map",
                       "Plan your journey through King's Cross St Pancras. Lifts and escalators.",
                       official=False)
RAIL_NOTICE = evidence("https://networkrail.co.uk/kings-cross-works",
                       "King's Cross upgrade engineering notice",
                       "Weekend engineering work closes platforms 9 to 11. Replacement buses run.",
                       official=False)
STATION_PAGE = evidence("https://nationalrail.co.uk/stations/kings-cross",
                        "King's Cross station information",
                        "Station facilities, ticket office hours and accessibility.",
                        official=False)
CAFE = evidence("https://thestandard.co.uk/", "The Standard, King's Cross",
                "Open daily 12:00-23:00. Kitchen closes at 22:00.", official=True)

TIMES = [{"start": "2026-09-19T15:00:00+01:00", "end": "2026-09-19T15:45:00+01:00", "phrase": "3"}]


def script(**over):
    base = {
        "classify": {"is_meeting_change": True},
        "extract": {"options": TIMES, "request_excerpt": "Could we meet near King's Cross?"},
        "query": {"query": "cafes near King's Cross open Friday opening hours",
                  "include_domains": []},
        "choose": {"venue_index": 0, "reason_sentence": "It is open at 15:00.", "rejected": [],
                   "is_venue": True},
        "draft": {"template_body": "Hi Alex,",
                  "fragments": [{"source": "time", "text": "15:00-15:45 works for me."}]},
    }
    base.update(over)
    return base


def harness(*, results=None, **kw):
    kw.setdefault("search", {"king's cross": results if results is not None
                             else [STATION_MAP, RAIL_NOTICE, STATION_PAGE, CAFE]})
    return Harness(messages=[change_email("m1")], threads={"t1": [change_email("m1")]},
                   script=script(**kw.pop("script", {})), **kw)


def prepared(h):
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready"
    return change


# ---------------------------------------------------------------- the query phase --------------
def test_the_query_phase_asks_for_a_venue_with_opening_hours():
    prompt = " ".join(ch.QUERY_SYSTEM.lower().split())
    assert "café" in prompt or "cafe" in prompt
    assert "restaurant" in prompt
    assert "opening hours" in prompt or "opening" in prompt


def test_the_query_phase_is_told_the_day_the_meeting_is_for():
    """Hours on the wrong day are not hours. The day is what makes the answer checkable."""
    prompt = " ".join(ch.QUERY_SYSTEM.lower().split())
    assert "day" in prompt
    h = harness()
    h.engine.prepare(h.engine.detect(None)[0].id)
    asked = h.rec.text("model")
    assert "2026-09-19" in asked, "the query payload no longer carries the day it is about"


def test_the_query_phase_is_told_what_a_venue_page_is_not():
    """The live results were about a station. The prompt names those, so a real model has the
    instruction the scripted one is graded against."""
    prompt = " ".join(ch.QUERY_SYSTEM.lower().split())
    assert "map" in prompt
    assert "station" in prompt or "transport" in prompt or "travel" in prompt


# ---------------------------------------------------------------- the choose phase -------------
def test_a_station_page_is_not_accepted_as_the_venue_it_is_asked_about():
    """The measured defect, at the point where it did the damage.

    The model picks the station map and is asked to say whether it is a venue. It says no, and the
    plan must not present it as a researched café.

    The name it falls back to is the meeting's OWN place and not "Venue to confirm": this event
    already has one ("Old Cafe, King's Cross"), the request asked for a new one, and the research
    found nothing usable — which is the case the round-2 review made an invariant, enforced in
    `changes.venue_for_write`. What this test measured is unchanged and is what the assertions
    below are about: the station map is not the venue, and nothing was researched to be evidence
    for anything."""
    h = harness(script={"choose": {"venue_index": 0, "is_venue": False,
                                   "reason_sentence": "The nearest result is the station.",
                                   "rejected": []}})
    change = prepared(h)
    venue = change.proposal["venue"]
    assert venue["evidence"] is None
    assert venue["name"] != STATION_MAP.title, "the station map became the venue"
    assert venue["name"] == "Old Cafe, King's Cross", "the meeting's own place was not kept"
    assert "not verified" in venue["note"] or "not a venue" in venue["note"]
    assert change.proposal["needs_choice"] is True


def test_the_refused_station_page_is_named_in_the_rejections_the_owner_reads():
    h = harness(script={"choose": {"venue_index": 0, "is_venue": False,
                                   "reason_sentence": "Nothing here is a venue.",
                                   "rejected": []}})
    change = prepared(h)
    reasons = " ".join(str(r.get("reason", "")) for r in change.proposal["rejected"])
    assert "venue" in reasons.lower()


def test_a_choice_that_never_says_whether_it_is_a_venue_is_a_loud_failure():
    """Required, because a missing `is_venue` is not the same answer as `false`: the engine would
    be discarding a choice the model did make."""
    h = harness(script={"choose": {"venue_index": 0, "reason_sentence": "It is open.",
                                   "rejected": []}})
    with pytest.raises(ch.ModelAnswerInvalid):
        h.engine.prepare(h.engine.detect(None)[0].id)


def test_the_choose_prompt_names_the_key_and_what_it_means():
    prompt = " ".join(ch.CHOOSE_SYSTEM.lower().split())
    assert '"is_venue"' in ch.CHOOSE_SYSTEM
    assert "map" in prompt
    assert "opening hours" in prompt


def test_a_real_cafe_is_still_chosen_and_still_carries_its_evidence():
    """The fix must not break the good case: an official café page is still the answer."""
    h = harness(script={"choose": {"venue_index": 3, "is_venue": True,
                                   "reason_sentence": "Its own page gives the hours.",
                                   "rejected": []}})
    venue = prepared(h).proposal["venue"]
    assert venue["name"] == "The Standard, King's Cross"
    assert venue["evidence"]["url"] == "https://thestandard.co.uk/"
    assert "seating not reserved" in venue["note"]


def test_choosing_none_is_an_answer_and_keeps_the_uncertainty():
    h = harness(script={"choose": {"venue_index": None, "is_venue": False,
                                   "reason_sentence": "No café page came back.",
                                   "rejected": []}})
    change = prepared(h)
    assert change.proposal["venue"]["evidence"] is None
    assert change.proposal["needs_choice"] is True


# ---------------------------------------------------------------- the bounds are unchanged -----
def test_the_two_round_cap_still_holds_after_the_query_was_sharpened():
    h = harness()
    h.engine.prepare(h.engine.detect(None)[0].id)
    assert len(h.search.queries) <= ch.MAX_SEARCH_ROUNDS
    assert ch.MAX_SEARCH_ROUNDS == 2


def test_every_query_still_goes_through_the_privacy_gate():
    """The sharpened query is a longer query, not an ungated one."""
    h = harness()
    h.engine.prepare(h.engine.detect(None)[0].id)
    assert isinstance(h.engine.search, GuardedSearch)
    asked = h.rec.text("search")
    for query in h.search.queries:
        assert query in asked, f"{query!r} never reached the recorded port"
