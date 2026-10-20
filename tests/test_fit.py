"""The deterministic validator: 16:00 must lose and 15:00 must win, for a stated reason.

This module is where the product's central demonstration is decided, so it is tested as a truth
table rather than through the flow. The 45-minute buffer is the OWNER'S RULE about their own
commitment — not a fabricated travel-time estimate — and the rejection has to say so.
"""
from __future__ import annotations

import datetime as dt

import pytest

from privatemesh.fit import Slot, check_options, check_slot
from privatemesh.ports import BusyInterval, ProtectedCommitment

from fakes_v2 import at

COMMITMENT = ProtectedCommitment(id="pc1", start=at("16:30"), end=at("17:30"),
                                 buffer_minutes=45, version=1)
NOW = dt.datetime.fromisoformat(at("09:00"))
DAY_START = at("08:00")
DAY_END = at("20:00")


def slot(start: str, end: str) -> Slot:
    return Slot(start=dt.datetime.fromisoformat(at(start)),
                end=dt.datetime.fromisoformat(at(end)))


def check(s: Slot, *, busy=(), protected=COMMITMENT):
    return check_slot(s, busy=list(busy), protected=protected, day_start=DAY_START,
                      day_end=DAY_END, now=NOW)


# ---------------------------------------------------------------- the canonical pair -----------
def test_1600_is_rejected_by_the_protected_buffer():
    """16:00 + 45 minutes ends 16:45; the boundary is 16:30 minus a 45-minute buffer = 15:45."""
    verdict = check(slot("16:00", "16:45"))
    assert not verdict.fits
    assert verdict.code == "overlaps_protected_buffer"
    assert "15:45" in verdict.reason and "16:30" in verdict.reason
    assert "your" in verdict.reason.lower()


def test_1500_is_accepted():
    verdict = check(slot("15:00", "15:45"))
    assert verdict.fits, verdict.reason
    assert verdict.code == "fits"


def test_the_buffer_boundary_is_inclusive_at_the_earlier_end():
    """Ending exactly on the latest allowed end is a fit; one minute past is not."""
    assert check(slot("15:00", "15:45")).fits
    assert not check(slot("15:01", "15:46")).fits
    assert check(slot("15:01", "15:46")).code == "overlaps_protected_buffer"


def test_the_commitment_itself_is_never_offered():
    verdict = check(slot("16:30", "17:15"))
    assert not verdict.fits
    assert verdict.code == "overlaps_protected"


def test_a_slot_after_the_commitment_is_checked_against_the_commitment_not_the_buffer():
    """The buffer sits BEFORE the commitment: 17:30 onwards is free (the 20:00 day end allows it)."""
    assert check(slot("17:30", "18:15")).fits
    assert not check(slot("17:15", "18:00")).fits


# ---------------------------------------------------------------- other refusals --------------
def test_a_busy_overlap_is_rejected_and_named():
    verdict = check(slot("15:00", "15:45"), busy=[BusyInterval(start=at("15:30"), end=at("16:00"))])
    assert not verdict.fits and verdict.code == "overlaps_busy"
    assert "15:30" in verdict.reason


def test_a_slot_in_the_past_is_rejected():
    assert check(slot("08:15", "09:00")).code == "in_the_past"


def test_a_slot_outside_the_day_is_rejected():
    assert check(slot("19:30", "20:15")).code == "outside_day"
    # Before the day starts, with no clock: an early slot is out of bounds, not merely past.
    early = check_slot(slot("07:00", "07:45"), protected=COMMITMENT, day_start=DAY_START,
                       day_end=DAY_END)
    assert early.code == "outside_day"


def test_a_zero_length_slot_is_rejected():
    assert check(slot("15:00", "15:00")).code == "empty_slot"


def test_a_reversed_slot_is_rejected():
    assert check(slot("15:45", "15:00")).code == "empty_slot"


def test_with_no_protected_commitment_the_buffer_rule_does_not_apply():
    assert check(slot("16:00", "16:45"), protected=None).fits


# ---------------------------------------------------------------- a list of options ------------
def test_check_options_keeps_order_and_reports_every_option():
    """The proposal shows the rejected options with their reasons, so nothing may be dropped."""
    results = check_options([slot("16:00", "16:45"), slot("15:00", "15:45")], busy=[],
                            protected=COMMITMENT, day_start=DAY_START, day_end=DAY_END, now=NOW)
    assert [r.fits for r in results] == [False, True]
    assert [r.code for r in results] == ["overlaps_protected_buffer", "fits"]
    assert results[0].slot.start == dt.datetime.fromisoformat(at("16:00"))


def test_a_timezone_offset_that_is_not_the_owners_is_still_compared_absolutely():
    """15:00 in +00:00 IS the canonical 16:00 in the owner's +01:00 day, so it must be refused by
    the same rule. A validator that compared clock faces would accept it."""
    other = Slot(start=dt.datetime.fromisoformat("2026-09-19T15:00:00+00:00"),
                 end=dt.datetime.fromisoformat("2026-09-19T15:45:00+00:00"))
    verdict = check(other)
    assert not verdict.fits
    assert verdict.code == "overlaps_protected_buffer"
    assert "15:45" in verdict.reason        # the local latest end, not the UTC one


def test_a_naive_datetime_is_refused_rather_than_assumed():
    naive = Slot(start=dt.datetime(2026, 9, 19, 15, 0), end=dt.datetime(2026, 9, 19, 15, 45))
    with pytest.raises(ValueError, match="timezone"):
        check(naive)


def test_today_is_what_the_watcher_asks_for():
    """Boundary at 16:30 with a 45-minute buffer: 16:00 is exactly the case the direction names."""
    assert check_options([slot("15:00", "15:45"), slot("16:00", "16:45")], busy=[], now=NOW,
                         protected=COMMITMENT, day_start=DAY_START, day_end=DAY_END)[0].fits
