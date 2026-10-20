"""The owner's held-out cases for a TAUGHT skill.

THE POINT OF THIS FILE IS WHERE IT LIVES, and the second point is what it does NOT say. It sits
outside the skill's directory, it was written before any generation ran, it is never put in a
prompt, and `skillrt.run_harness` refuses the arrangement where the code being graded could rewrite
it.

What it deliberately does not assert is that a slot FITS. A taught rule narrows, so a case like
"15:00 is free" is true for one rule and false for another, and a held-out set that pinned it would
grade every taught skill against one owner's sentence. What it asserts instead is the contract that
holds for EVERY taught rule — the same facts the invariant cases check in `teach.py`, restated here
in the one place the generated code cannot reach:

  * one option per offered slot, in the order offered, including the refused ones;
  * `ranked` carries exactly the options that fit, earliest first, and nothing else;
  * a slot the trusted validator refuses stays refused — the rule may take options away and can
    never put one back;
  * no option is invented for a slot nobody offered, and no venue is invented for a listing nobody
    stands behind.

This file proves the shape and the direction, not the rule. Whether the rule the code implements is
the rule the owner meant is decided by the owner reading the labelled examples on the approval
screen, and nothing in this directory can substitute for that.
"""
from __future__ import annotations

import taught_rule

#: A day and a working window the cases below sit inside, so `outside_day` is never the reason a
#: case fails and the assertions are about the rule rather than about the clock.
DAY = "2026-09-21"
TZ = "+01:00"


def _at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00{TZ}"


def _slot(start: str, end: str) -> dict:
    return {"start": _at(start), "end": _at(end)}


def _day() -> dict:
    return {"day_start": _at("09:00"), "day_end": _at("18:00"), "now": _at("08:00")}


def _protected() -> dict:
    return {"start": _at("16:30"), "end": _at("17:30"), "buffer_minutes": 45}


def test_it_reports_every_offered_slot_once_in_the_order_offered():
    offered = [_slot("15:00", "15:45"), _slot("09:30", "10:15"), _slot("13:00", "13:45")]
    result = taught_rule.run({"offered_slots": offered, **_day()})
    assert len(result["options"]) == 3
    assert [o["start"] for o in result["options"]] == [s["start"] for s in offered]


def test_ranked_carries_exactly_the_options_that_fit_earliest_first():
    result = taught_rule.run({"offered_slots": [_slot("15:00", "15:45"), _slot("13:00", "13:45")],
                              **_day()})
    fitting = sorted(o["start"] for o in result["options"] if o["fits"])
    assert [r["start"] for r in result["ranked"]] == fitting


def test_a_slot_the_buffer_refuses_stays_refused_whatever_the_rule_says():
    """The one direction that is never allowed. 16:00–16:45 ends at 16:45, inside the 45 minutes
    kept before the 16:30 commitment, so `fit.py` refuses it — and no owner's rule can turn that
    into an acceptance, because the rule runs AFTER the trusted rules and can only subtract."""
    result = taught_rule.run({"offered_slots": [_slot("16:00", "16:45")], "protected": _protected(),
                              **_day()})
    assert result["options"][0]["fits"] is False
    assert result["options"][0]["code"] == "overlaps_protected_buffer"
    assert result["ranked"] == []


def test_a_slot_inside_the_protected_commitment_stays_refused():
    result = taught_rule.run({"offered_slots": [_slot("16:45", "17:30")], "protected": _protected(),
                              **_day()})
    assert result["options"][0]["fits"] is False
    assert result["options"][0]["code"] == "overlaps_protected"


def test_a_slot_overlapping_something_booked_stays_refused():
    result = taught_rule.run({"offered_slots": [_slot("11:30", "12:15")],
                              "busy": [{"start": _at("11:15"), "end": _at("12:00")}], **_day()})
    assert result["options"][0]["fits"] is False
    assert result["options"][0]["code"] == "overlaps_busy"


def test_a_slot_outside_the_working_day_stays_refused():
    """`now` is 06:00 rather than the usual 08:00 so that both slots are refused for being outside
    the day rather than for having passed: 07:45 is in the past by 08:00, and `fit.py` checks
    `in_the_past` first. The point of the case is the day boundary, so the clock is moved out of
    the way of the thing being asserted."""
    result = taught_rule.run({"offered_slots": [_slot("07:00", "07:45"), _slot("17:30", "18:15")],
                              "day_start": _at("09:00"), "day_end": _at("18:00"),
                              "now": _at("06:00")})
    assert [o["code"] for o in result["options"]] == ["outside_day", "outside_day"]


def test_a_slot_that_has_already_passed_stays_refused():
    result = taught_rule.run({"offered_slots": [_slot("09:00", "09:45")], "day_start": _at("09:00"),
                              "day_end": _at("18:00"), "now": _at("11:00")})
    assert result["options"][0]["code"] == "in_the_past"


def test_it_never_invents_an_option_for_a_slot_nobody_offered():
    result = taught_rule.run({"offered_slots": [_slot("15:00", "15:45")], **_day()})
    assert {o["start"] for o in result["options"]} == {_at("15:00")}


def test_no_official_listing_means_no_venue_rather_than_a_guess():
    result = taught_rule.run({"offered_slots": [_slot("15:00", "15:45")], **_day(),
                              "venues": [{"name": "Some Blog", "title": "10 cafés",
                                          "url": "https://blog.example/", "official": False}]})
    assert all(r["venue"] is None for r in result["ranked"])


def test_an_official_listing_is_the_one_that_is_attached():
    result = taught_rule.run({
        "offered_slots": [_slot("15:00", "15:45")], **_day(),
        "venues": [{"name": "Some Blog", "title": "10 cafés", "url": "https://blog.example/",
                    "official": False},
                   {"name": "Fern & Finch Café", "title": "Fern & Finch, King's Cross",
                    "url": "https://fernandfinch.example/", "official": True}]})
    assert all(r["venue"]["name"] == "Fern & Finch Café" for r in result["ranked"])


def test_every_refusal_carries_a_sentence_naming_a_clock_time():
    """A refusal the owner cannot check is indistinguishable from a bug, and the reason is the only
    thing on the screen that lets them check it."""
    import re as _re

    offered = [_slot("07:00", "07:45"), _slot("11:30", "12:15"), _slot("16:00", "16:45"),
               _slot("15:00", "15:00"), _slot("13:00", "13:45")]
    result = taught_rule.run({"offered_slots": offered, "protected": _protected(), **_day(),
                              "busy": [{"start": _at("11:15"), "end": _at("12:00")}]})
    for option in result["options"]:
        if not option["fits"]:
            assert _re.search(r"\d{2}:\d{2}", str(option["reason"])), option
