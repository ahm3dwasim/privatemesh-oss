"""Does this slot fit the day? A deterministic validator, and the reason the demo has a decision.

The direction's canonical moment is a rejected 16:00: the owner has a protected commitment at
16:30 and a 45-minute buffer, so a 45-minute meeting starting at 16:00 would end at 16:45 and run
into the boundary. 15:00–15:45 fits. Nothing here calls a model. That is the point — the private
constraint changes the answer through arithmetic the owner can check, not through a prompt the
owner has to trust.

The buffer is the OWNER'S RULE about their own commitment, applied BEFORE it, not a fabricated
travel-time estimate and not padding after it.

Every refusal carries a `code` (for tests and for the UI to branch on) and a `reason` in plain
language that names the actual numbers, because a rejection the owner cannot verify is
indistinguishable from a bug.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from .ports import BusyInterval, ProtectedCommitment

FITS = "fits"
EMPTY_SLOT = "empty_slot"
IN_THE_PAST = "in_the_past"
OUTSIDE_DAY = "outside_day"
OVERLAPS_PROTECTED = "overlaps_protected"
OVERLAPS_BUFFER = "overlaps_protected_buffer"
OVERLAPS_BUSY = "overlaps_busy"
NO_TIMEZONE = "no_timezone"


@dataclass(frozen=True)
class Slot:
    start: _dt.datetime
    end: _dt.datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class Fit:
    slot: Slot
    fits: bool
    code: str
    reason: str


def _aware(moment: _dt.datetime | str, what: str) -> _dt.datetime:
    """Every comparison happens between aware instants.

    A naive datetime is refused rather than assumed: guessing a timezone is how a 15:00 meeting
    becomes a 14:00 meeting, and the whole product is a claim about a specific hour."""
    when = _dt.datetime.fromisoformat(moment) if isinstance(moment, str) else moment
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError(f"{what} has no timezone: a naive datetime cannot be placed in a day")
    return when


def _hhmm(moment: _dt.datetime) -> str:
    return moment.strftime("%H:%M")


def check_slot(slot: Slot, *, busy=(), protected: ProtectedCommitment | None = None,
               day_start: _dt.datetime | str | None = None,
               day_end: _dt.datetime | str | None = None,
               now: _dt.datetime | str | None = None) -> Fit:
    """The verdict for one proposed slot. Pure: same inputs, same answer, no clock of its own."""
    start, end = _aware(slot.start, "the proposed start"), _aware(slot.end, "the proposed end")
    slot = Slot(start=start, end=end)

    if end <= start:
        return Fit(slot, False, EMPTY_SLOT,
                   f"{_hhmm(start)}–{_hhmm(end)} is not a meeting: it ends before it starts")

    if now is not None and end <= _aware(now, "now"):
        return Fit(slot, False, IN_THE_PAST, f"{_hhmm(start)} has already passed")

    if day_start is not None and start < _aware(day_start, "day_start"):
        return Fit(slot, False, OUTSIDE_DAY, f"{_hhmm(start)} is before the day starts")
    if day_end is not None and end > _aware(day_end, "day_end"):
        return Fit(slot, False, OUTSIDE_DAY, f"{_hhmm(end)} is after the day ends")

    if protected is not None:
        p_start = _aware(protected.start, "the protected start")
        p_end = _aware(protected.end, "the protected end")
        buffer = max(0, int(protected.buffer_minutes))
        # The unavailable region is the commitment PLUS the buffer before it, and it ends at
        # p_end: a slot after the commitment is not "inside the buffer", it is simply later in
        # the day. Testing the two zones separately is what keeps 17:30 from being refused by a
        # rule about 15:45.
        buffer_start = p_start - _dt.timedelta(minutes=buffer)
        if start < p_start and end > buffer_start:
            return Fit(
                slot, False, OVERLAPS_BUFFER,
                f"{_hhmm(start)}–{_hhmm(end)} would run into the buffer you keep before your "
                f"{_hhmm(p_start)} commitment: the latest this can end is {_hhmm(buffer_start)} "
                f"({buffer} minutes of buffer)")
        if start < p_end and end > p_start:
            return Fit(slot, False, OVERLAPS_PROTECTED,
                       f"{_hhmm(start)}–{_hhmm(end)} runs into your protected "
                       f"{_hhmm(p_start)}–{_hhmm(p_end)} commitment")

    for window in busy:
        b_start = _aware(window.start, "a busy start")
        b_end = _aware(window.end, "a busy end")
        if start < b_end and end > b_start:
            return Fit(slot, False, OVERLAPS_BUSY,
                       f"{_hhmm(start)}–{_hhmm(end)} overlaps something already booked at "
                       f"{_hhmm(b_start)}–{_hhmm(b_end)}")

    return Fit(slot, True, FITS, f"{_hhmm(start)}–{_hhmm(end)} is free")


def check_options(options, **kw) -> list[Fit]:
    """Every option, in the order it was offered — the proposal has to show the rejected ones and
    say why, so dropping a losing option is not a simplification this function is allowed."""
    return [check_slot(slot, **kw) for slot in options]


def latest_end(protected: ProtectedCommitment | None, *, fallback: _dt.datetime) -> _dt.datetime:
    """The last instant a meeting may end: the protected start minus its buffer."""
    if protected is None:
        return fallback
    return _aware(protected.start, "the protected start") - _dt.timedelta(
        minutes=max(0, int(protected.buffer_minutes)))


def as_busy(protected: ProtectedCommitment | None) -> list[BusyInterval]:
    """The protected commitment as a busy interval, for a caller that only speaks free/busy.

    The commitment plus its buffer is exactly the region the owner is unavailable. Nothing about
    WHAT it is travels with it."""
    if protected is None:
        return []
    start = _aware(protected.start, "the protected start") - _dt.timedelta(
        minutes=max(0, int(protected.buffer_minutes)))
    return [BusyInterval(start=start.isoformat(), end=protected.end)]
