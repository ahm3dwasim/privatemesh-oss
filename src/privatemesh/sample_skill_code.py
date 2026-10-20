"""The scripted generator and reviewer, for sample mode.

A judge has no API key and must not cost anything, so the two model calls the learning loop makes
are answered from here — but they are answered as the REAL prompts ask, and the routing is on the
prompt's CONTENT. There is no `if sample:` anywhere in `learn.py`; `sample_reply` decides what to
answer by reading what it was sent, exactly as it does for the engine's phases.

The two artifacts are the demo:

* **v1 overreaches.** It imports `urllib` at module scope, which the runtime refuses by name before
  the skill can run a line, and its own tests and the owner's held-out tests therefore fail with
  that violation attached. The reviewer rejects it for the same reason.
* **v2 computes.** It implements the contract as written, passes the coder's own tests and the
  held-out set it was never shown, and is approved.

The interesting claim is not that v2 exists — it is that v1 could not be quietly kept: the runtime
named the escape, the held-out set could not be edited by the code it graded, and a different
model family said no. Delete either of those two and the sample stops demonstrating the product.

Nothing here is imported by the live path.
"""
from __future__ import annotations

import re

#: v1 — the overreach. `import urllib.request` is the first line of a module that intended to fetch
#: the owner's whole calendar instead of using the inputs it was handed. The import alone is fatal:
#: `skillrt`'s guard refuses it by name at import time, before the entry point exists.
V1_MODULE = '''"""Rank the meeting times someone offered against the owner's day."""
import urllib.request

CALENDAR_FEED = "https://www.googleapis.com/calendar/v3/users/me/calendarList"


def _whole_calendar():
    # "Read everything and filter later" — the shortcut this skill is not allowed to take.
    with urllib.request.urlopen(CALENDAR_FEED) as response:
        return response.read()


def run(inputs):
    _whole_calendar()
    slots = list((inputs or {}).get("offered_slots") or ())
    return {"options": [{"start": s.get("start"), "end": s.get("end"), "fits": True,
                         "code": "fits", "reason": "it looked free"} for s in slots],
            "ranked": [{"start": s.get("start"), "end": s.get("end"), "venue": None}
                       for s in slots]}
'''

V1_TESTS = '''"""Tests for the option evaluator."""
import meeting_option_evaluator


def test_it_returns_one_option_per_offered_slot():
    result = meeting_option_evaluator.run({"offered_slots": [
        {"start": "2026-09-19T15:00:00+01:00", "end": "2026-09-19T15:45:00+01:00"}]})
    assert len(result["options"]) == 1


def test_a_free_slot_is_ranked():
    result = meeting_option_evaluator.run({"offered_slots": [
        {"start": "2026-09-19T15:00:00+01:00", "end": "2026-09-19T15:45:00+01:00"}]})
    assert len(result["ranked"]) == 1
'''

#: v1's manifest is well-formed and says all the right things. The code does not. That gap IS the
#: product's claim: a manifest is a claim by the generator, and the runtime is what makes it true
#: or visible. A manifest that refused to parse would fail for a different and much less
#: interesting reason — the attempt would die in `learn` before the runtime ever saw the code.
V1_MANIFEST = {
    "inputs": None,   # filled by generation_envelope from the trusted schema
    "outputs": None,
    "reads": ["offered_slots", "duration_minutes", "minimum_notice_minutes", "busy", "protected",
              "venues", "day_start", "day_end", "now"],
    "proposes": [],
    "never": ["read the owner's calendar directly"],
}

#: v2 — the same capability, computed from the inputs it was handed. The reason strings name the
#: clock times, for the same reason `fit.py`'s do: a rejection the owner cannot check is
#: indistinguishable from a bug.
V2_MODULE = '''"""Rank the meeting times someone offered against the owner\'s day.

Pure function over the inputs it is handed. It reads nothing else: no network, no files, no clock
of its own, and no key it was not given.
"""
import datetime as _dt

FITS = "fits"
EMPTY_SLOT = "empty_slot"
IN_THE_PAST = "in_the_past"
OUTSIDE_DAY = "outside_day"
OVERLAPS_PROTECTED = "overlaps_protected"
OVERLAPS_BUFFER = "overlaps_protected_buffer"
OVERLAPS_BUSY = "overlaps_busy"
WRONG_DURATION = "wrong_duration"
TOO_SOON = "too_soon"


def _when(value, what):
    moment = _dt.datetime.fromisoformat(str(value))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("%s has no timezone: %r" % (what, value))
    return moment


def _hhmm(moment):
    return moment.strftime("%H:%M")


def _judge(offered, protected, busy, day_start, day_end, now, duration, notice):
    start = _when(offered["start"], "an offered start")
    end = _when(offered["end"], "an offered end")

    if end <= start:
        return start, end, False, EMPTY_SLOT, "%s\\u2013%s is not a meeting: it ends before it starts" % (
            _hhmm(start), _hhmm(end))
    if now is not None and end <= now:
        return start, end, False, IN_THE_PAST, "%s has already passed" % _hhmm(start)
    if day_start is not None and start < day_start:
        return start, end, False, OUTSIDE_DAY, "%s is before the day starts" % _hhmm(start)
    if day_end is not None and end > day_end:
        return start, end, False, OUTSIDE_DAY, "%s is after the day ends" % _hhmm(end)

    if protected is not None:
        p_start, p_end, buffer_minutes = protected
        buffer_start = p_start - _dt.timedelta(minutes=buffer_minutes)
        if start < p_start and end > buffer_start:
            return start, end, False, OVERLAPS_BUFFER, (
                "%s\\u2013%s would run into the buffer you keep before your %s commitment: the "
                "latest this can end is %s (%d minutes of buffer)" % (
                    _hhmm(start), _hhmm(end), _hhmm(p_start), _hhmm(buffer_start), buffer_minutes))
        if start < p_end and end > p_start:
            return start, end, False, OVERLAPS_PROTECTED, (
                "%s\\u2013%s runs into your protected %s\\u2013%s commitment" % (
                    _hhmm(start), _hhmm(end), _hhmm(p_start), _hhmm(p_end)))

    for window in busy or ():
        b_start = _when(window["start"], "a busy start")
        b_end = _when(window["end"], "a busy end")
        if start < b_end and end > b_start:
            return start, end, False, OVERLAPS_BUSY, (
                "%s\\u2013%s overlaps something already booked at %s\\u2013%s" % (
                    _hhmm(start), _hhmm(end), _hhmm(b_start), _hhmm(b_end)))

    minutes = int((end - start).total_seconds() // 60)
    if duration > 0 and minutes != duration:
        return start, end, False, WRONG_DURATION, (
            "%s\\u2013%s is %d minutes, not the %d minutes asked for" % (
                _hhmm(start), _hhmm(end), minutes, duration))
    if notice > 0 and now is not None and start < now + _dt.timedelta(minutes=notice):
        return start, end, False, TOO_SOON, (
            "%s is less than the %d minutes of notice you need before a meeting" % (
                _hhmm(start), notice))

    return start, end, True, FITS, "%s\\u2013%s is free" % (_hhmm(start), _hhmm(end))


def _venue(venues):
    for venue in venues or ():
        if isinstance(venue, dict) and venue.get("official"):
            return {"name": str(venue.get("name") or venue.get("title") or ""),
                    "url": str(venue.get("url") or ""),
                    "title": str(venue.get("title") or "")}
    return None


def run(inputs):
    inputs = inputs or {}
    duration = int(inputs.get("duration_minutes") or 0)
    notice = int(inputs.get("minimum_notice_minutes") or 0)
    now = _when(inputs["now"], "now") if inputs.get("now") else None
    day_start = _when(inputs["day_start"], "day_start") if inputs.get("day_start") else None
    day_end = _when(inputs["day_end"], "day_end") if inputs.get("day_end") else None

    protected = None
    fact = inputs.get("protected")
    if isinstance(fact, dict) and fact.get("start") and fact.get("end"):
        protected = (_when(fact["start"], "the protected start"),
                     _when(fact["end"], "the protected end"),
                     max(0, int(fact.get("buffer_minutes") or 0)))

    options = []
    for offered in inputs.get("offered_slots") or ():
        start, end, fits, code, reason = _judge(
            offered, protected, inputs.get("busy"), day_start, day_end, now, duration, notice)
        options.append({"start": start.isoformat(), "end": end.isoformat(), "fits": fits,
                        "code": code, "reason": reason})

    venue = _venue(list(inputs.get("venues") or ()))
    ranked = [{"start": option["start"], "end": option["end"], "venue": venue}
              for option in sorted((o for o in options if o["fits"]),
                                   key=lambda o: o["start"])]
    return {"options": options, "ranked": ranked}
'''

V2_TESTS = '''"""Tests for the option evaluator, written by the model that wrote it.

These are the coder's own cases. They are necessary and they are not the evidence — the held-out
set the coder never saw is.
"""
import meeting_option_evaluator

DAY = "2026-09-19"
TZ = "+01:00"


def _at(hhmm):
    return "%sT%s:00%s" % (DAY, hhmm, TZ)


def _protected():
    return {"start": _at("16:30"), "end": _at("17:30"), "buffer_minutes": 45}


def test_a_slot_inside_the_protected_buffer_does_not_fit():
    result = meeting_option_evaluator.run({
        "offered_slots": [{"start": _at("16:00"), "end": _at("16:45")}],
        "protected": _protected()})
    assert result["options"][0]["fits"] is False
    assert result["options"][0]["code"] == "overlaps_protected_buffer"
    assert result["ranked"] == []


def test_a_free_slot_fits_and_is_ranked():
    result = meeting_option_evaluator.run({
        "offered_slots": [{"start": _at("15:00"), "end": _at("15:45")}],
        "protected": _protected()})
    assert result["options"][0]["fits"] is True
    assert [r["start"] for r in result["ranked"]] == [_at("15:00")]


def test_fitting_slots_are_ranked_earliest_first():
    result = meeting_option_evaluator.run({
        "offered_slots": [{"start": _at("17:00"), "end": _at("17:45")},
                          {"start": _at("14:00"), "end": _at("14:45")}]})
    assert [r["start"] for r in result["ranked"]] == [_at("14:00"), _at("17:00")]


def test_a_slot_that_has_already_passed_does_not_fit():
    result = meeting_option_evaluator.run({
        "offered_slots": [{"start": _at("09:00"), "end": _at("09:45")}],
        "now": _at("11:00")})
    assert result["options"][0]["code"] == "in_the_past"


def test_an_official_venue_is_attached_to_the_ranked_option():
    result = meeting_option_evaluator.run({
        "offered_slots": [{"start": _at("15:00"), "end": _at("15:45")}],
        "venues": [{"name": "The Standard", "url": "https://thestandard.co.uk/",
                    "title": "The Standard, King's Cross", "official": True}]})
    assert result["ranked"][0]["venue"]["name"] == "The Standard"


def test_no_official_venue_means_no_venue_rather_than_a_guess():
    result = meeting_option_evaluator.run({
        "offered_slots": [{"start": _at("15:00"), "end": _at("15:45")}],
        "venues": [{"name": "Some Blog", "url": "https://blog.example/",
                    "title": "10 cafes", "official": False}]})
    assert result["ranked"][0]["venue"] is None


def test_every_offered_slot_is_reported_even_the_rejected_ones():
    result = meeting_option_evaluator.run({
        "offered_slots": [{"start": _at("16:00"), "end": _at("16:45")},
                          {"start": _at("15:00"), "end": _at("15:45")}],
        "protected": _protected()})
    assert len(result["options"]) == 2
    assert result["options"][1]["fits"] is True
'''

V2_MANIFEST = {
    "inputs": None,
    "outputs": None,
    "reads": ["offered_slots", "duration_minutes", "minimum_notice_minutes", "busy", "protected",
              "venues", "day_start", "day_end", "now"],
    "proposes": [],
    "never": ["read the owner's calendar, email or files",
              "use the network",
              "move or send anything",
              "keep anything between runs"],
}


def generation_envelope(payload: dict) -> dict:
    """The generator's answer, chosen by what the REQUEST contains.

    The first attempt is asked for a capability that does not exist yet; every later one is handed
    the previous attempt's failures. That is the whole routing rule — no flag, no counter, and a
    request that stops carrying a previous attempt stops getting the revision."""
    revising = isinstance(payload.get("previous_attempt"), dict)
    inputs = payload.get("inputs_schema") or {}
    outputs = payload.get("outputs_schema") or {}
    code, tests, declared = ((V2_MODULE, V2_TESTS, V2_MANIFEST) if revising
                             else (V1_MODULE, V1_TESTS, V1_MANIFEST))
    return {"module_code": code, "tests_code": tests,
            "manifest": {**declared, "inputs": inputs, "outputs": outputs}}


# ---------------------------------------------------------------- Teach Mode ---------------------
"""The scripted answers for a TAUGHT rule — `POST /v2/teach`.

A judge types a rule and has no API key, so this fixture answers the taught prompts too. It is a
fixture with a stated limit, and the limit is the honest part:

**The sample's scripted model understands two shapes of rule** — a time-of-day floor ("never before
10am", "nothing until 9:30") and a weekday ban ("no meetings on Friday afternoons"). It reads them
out of the owner's sentence with a small deterministic parser and writes code that implements
exactly that. A rule it cannot read produces NO envelope at all, which the loop reports as a named
generation failure rather than as a skill that quietly implements something else. That is the whole
point of a sample: the alternative — returning the one rule it knows for every sentence it is given
— would demonstrate Teach Mode ignoring what the owner typed, which is the failure mode this
feature exists to prevent.

A live run has no such limit: the rule goes to a real Nemotron verbatim, as `owner_rule`.
"""

#: The two shapes, in the owner's words. Deliberately small and readable rather than clever.
_FLOOR_RE = re.compile(r"\b(?:before|until|till|earlier than|prior to)\s+(\d{1,2})(?::(\d{2}))?\s*"
                       r"(am|pm)?\b", re.IGNORECASE)
_CEILING_RE = re.compile(r"\b(?:after|later than|past)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
                         re.IGNORECASE)
_DAY_WORDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

#: A week of real dates, so a weekday example names a day that actually IS that weekday. Monday
#: first, matching `datetime.weekday()`.
_WEEK = ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26",
         "2026-09-27")


def _clock(hour: int, minute: int, meridiem: str | None) -> str:
    """`10`, `am` -> `10:00`; `5`, `pm` -> `17:00`. Clamped rather than guessed at."""
    hour = max(0, min(23, int(hour)))
    if meridiem:
        if meridiem.lower() == "pm" and hour < 12:
            hour += 12
        if meridiem.lower() == "am" and hour == 12:
            hour = 0
    return "%02d:%02d" % (hour, int(minute or 0))


def parse_taught_rule(rule: str) -> dict:
    """The sample's reading of the owner's sentence: what it refuses, and whether it understood.

    `understood` is False when neither shape is present. The caller then answers with no envelope —
    a named failure — instead of a skill that implements a rule nobody wrote.
    """
    text = str(rule or "")
    floor = _FLOOR_RE.search(text)
    ceiling = _CEILING_RE.search(text)
    days = tuple(i for i, name in enumerate(_DAY_WORDS)
                 if re.search(r"\b%s\b" % name, text, re.IGNORECASE))
    return {"floor": _clock(floor.group(1), floor.group(2), floor.group(3)) if floor else "",
            "ceiling": _clock(ceiling.group(1), ceiling.group(2), ceiling.group(3))
            if ceiling else "",
            "days": days,
            "understood": bool(floor or ceiling or days)}


#: The taught module. It is `V2_MODULE`'s logic — the trusted rules, applied exactly and first —
#: plus one narrowing step at the end, which is the owner's rule. The placeholders are the parsed
#: rule; the structure is the contract, and the ORDER is the safety argument: narrow last, and only
#: over a slot the rules above already accepted.
TAUGHT_MODULE_TEMPLATE = '''"""Apply the rule the owner taught, on top of the rules that always apply.

Pure function over the inputs it is handed. The rules in `rules_in_order` decide first, unchanged;
the owner\'s rule only ever takes options AWAY from what those rules allowed. It can never put one
back, which is the only reason generated code is allowed to decide anything here.
"""
import datetime as _dt

FITS = "fits"
EMPTY_SLOT = "empty_slot"
IN_THE_PAST = "in_the_past"
OUTSIDE_DAY = "outside_day"
OVERLAPS_PROTECTED = "overlaps_protected"
OVERLAPS_BUFFER = "overlaps_protected_buffer"
OVERLAPS_BUSY = "overlaps_busy"
WRONG_DURATION = "wrong_duration"
TOO_SOON = "too_soon"
TAUGHT_RULE = "taught_rule"

#: The owner\'s rule, as the sample\'s scripted model read it. Empty string means "no such bound".
OWNER_RULE = %(rule)r
FLOOR = %(floor)r
CEILING = %(ceiling)r
DAYS_REFUSED = %(days)r


def _when(value, what):
    moment = _dt.datetime.fromisoformat(str(value))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("%%s has no timezone: %%r" %% (what, value))
    return moment


def _hhmm(moment):
    return moment.strftime("%%H:%%M")


def _judge(offered, protected, busy, day_start, day_end, now, duration, notice):
    start = _when(offered["start"], "an offered start")
    end = _when(offered["end"], "an offered end")

    if end <= start:
        return start, end, False, EMPTY_SLOT, "%%s\\u2013%%s is not a meeting: it ends before it starts" %% (
            _hhmm(start), _hhmm(end))
    if now is not None and end <= now:
        return start, end, False, IN_THE_PAST, "%%s has already passed" %% _hhmm(start)
    if day_start is not None and start < day_start:
        return start, end, False, OUTSIDE_DAY, "%%s is before the day starts" %% _hhmm(start)
    if day_end is not None and end > day_end:
        return start, end, False, OUTSIDE_DAY, "%%s is after the day ends" %% _hhmm(end)

    if protected is not None:
        p_start, p_end, buffer_minutes = protected
        buffer_start = p_start - _dt.timedelta(minutes=buffer_minutes)
        if start < p_start and end > buffer_start:
            return start, end, False, OVERLAPS_BUFFER, (
                "%%s\\u2013%%s would run into the buffer you keep before your %%s commitment: the "
                "latest this can end is %%s (%%d minutes of buffer)" %% (
                    _hhmm(start), _hhmm(end), _hhmm(p_start), _hhmm(buffer_start), buffer_minutes))
        if start < p_end and end > p_start:
            return start, end, False, OVERLAPS_PROTECTED, (
                "%%s\\u2013%%s runs into your protected %%s\\u2013%%s commitment" %% (
                    _hhmm(start), _hhmm(end), _hhmm(p_start), _hhmm(p_end)))

    for window in busy or ():
        b_start = _when(window["start"], "a busy start")
        b_end = _when(window["end"], "a busy end")
        if start < b_end and end > b_start:
            return start, end, False, OVERLAPS_BUSY, (
                "%%s\\u2013%%s overlaps something already booked at %%s\\u2013%%s" %% (
                    _hhmm(start), _hhmm(end), _hhmm(b_start), _hhmm(b_end)))

    minutes = int((end - start).total_seconds() // 60)
    if duration > 0 and minutes != duration:
        return start, end, False, WRONG_DURATION, (
            "%%s\\u2013%%s is %%d minutes, not the %%d minutes asked for" %% (
                _hhmm(start), _hhmm(end), minutes, duration))
    if notice > 0 and now is not None and start < now + _dt.timedelta(minutes=notice):
        return start, end, False, TOO_SOON, (
            "%%s is less than the %%d minutes of notice you need before a meeting" %% (
                _hhmm(start), notice))

    return start, end, True, FITS, "%%s\\u2013%%s is free" %% (_hhmm(start), _hhmm(end))


def _owner_rule(start):
    """The owner\'s rule, applied ONLY to a slot every rule above already accepted.

    Returns None when the slot stands, or the sentence explaining why it does not. It takes options
    away and can do nothing else: there is no branch in this function that can turn a refusal into
    an acceptance, which is the property the invariant check and the run-time guard both look for.
    """
    if FLOOR and _hhmm(start) < FLOOR:
        return "your rule refuses anything before %%s: %%s is earlier than that" %% (FLOOR,
                                                                                    _hhmm(start))
    if CEILING and _hhmm(start) > CEILING:
        return "your rule refuses anything after %%s: %%s is later than that" %% (CEILING,
                                                                                  _hhmm(start))
    if start.weekday() in DAYS_REFUSED:
        return "your rule refuses %%s on a %%s: this %%s is a %%s" %% (
            _hhmm(start), start.strftime("%%A"), _hhmm(start), start.strftime("%%A"))
    return None


def _venue(venues):
    for venue in venues or ():
        if isinstance(venue, dict) and venue.get("official"):
            return {"name": str(venue.get("name") or venue.get("title") or ""),
                    "url": str(venue.get("url") or ""),
                    "title": str(venue.get("title") or "")}
    return None


def run(inputs):
    inputs = inputs or {}
    duration = int(inputs.get("duration_minutes") or 0)
    notice = int(inputs.get("minimum_notice_minutes") or 0)
    now = _when(inputs["now"], "now") if inputs.get("now") else None
    day_start = _when(inputs["day_start"], "day_start") if inputs.get("day_start") else None
    day_end = _when(inputs["day_end"], "day_end") if inputs.get("day_end") else None

    protected = None
    fact = inputs.get("protected")
    if isinstance(fact, dict) and fact.get("start") and fact.get("end"):
        protected = (_when(fact["start"], "the protected start"),
                     _when(fact["end"], "the protected end"),
                     max(0, int(fact.get("buffer_minutes") or 0)))

    options = []
    for offered in inputs.get("offered_slots") or ():
        start, end, fits, code, reason = _judge(
            offered, protected, inputs.get("busy"), day_start, day_end, now, duration, notice)
        if fits:
            # LAST, and only over a slot that already fits. The narrowing can move `fits` one way.
            refusal = _owner_rule(start)
            if refusal is not None:
                fits, code, reason = False, TAUGHT_RULE, refusal
        options.append({"start": start.isoformat(), "end": end.isoformat(), "fits": fits,
                        "code": code, "reason": reason})

    venue = _venue(list(inputs.get("venues") or ()))
    ranked = [{"start": option["start"], "end": option["end"], "venue": venue}
              for option in sorted((o for o in options if o["fits"]), key=lambda o: o["start"])]
    return {"options": options, "ranked": ranked}
'''

#: The taught module\'s overreach, for the same reason `V1_MODULE` exists: the sample has to show
#: that a first attempt which reaches outside its declaration is STOPPED, not quietly kept. It is
#: the capability\'s v1 with the taught constants bolted on, so the refusal the owner sees is the
#: runtime naming `socket`, and the second attempt is the one that computes.
TAUGHT_V1_MODULE = '''"""Apply the rule the owner taught."""
import socket


def _reach_out():
    # The shortcut a taught skill is never allowed: ask something else what the answer is.
    socket.create_connection(("calendar.google.com", 443))


def run(inputs):
    _reach_out()
    slots = list((inputs or {}).get("offered_slots") or ())
    return {"options": [{"start": s.get("start"), "end": s.get("end"), "fits": True,
                         "code": "fits", "reason": "it looked free"} for s in slots],
            "ranked": [{"start": s.get("start"), "end": s.get("end"), "venue": None}
                       for s in slots]}
'''

TAUGHT_TESTS = '''"""Tests for the taught narrowing skill, written by the model that wrote it."""
import taught_rule


def _at(hhmm):
    return "2026-09-21T%s:00+01:00" % hhmm


def test_a_slot_the_trusted_rules_refuse_stays_refused():
    result = taught_rule.run({
        "offered_slots": [{"start": _at("16:00"), "end": _at("16:45")}],
        "protected": {"start": _at("16:30"), "end": _at("17:30"), "buffer_minutes": 45}})
    assert result["options"][0]["fits"] is False
    assert result["options"][0]["code"] == "overlaps_protected_buffer"


def test_every_offered_slot_is_reported_once():
    result = taught_rule.run({"offered_slots": [
        {"start": _at("09:30"), "end": _at("10:15")},
        {"start": _at("15:00"), "end": _at("15:45")}]})
    assert len(result["options"]) == 2


def test_the_ranked_list_only_carries_slots_that_still_fit():
    result = taught_rule.run({"offered_slots": [
        {"start": _at("09:30"), "end": _at("10:15")},
        {"start": _at("15:00"), "end": _at("15:45")}]})
    assert [o["start"] for o in result["ranked"]] == [
        s for s, o in zip([_at("09:30"), _at("15:00")], result["options"]) if o["fits"]]
'''

TAUGHT_MANIFEST = {
    "inputs": None,
    "outputs": None,
    "reads": ["offered_slots", "duration_minutes", "minimum_notice_minutes", "busy", "protected",
              "venues", "day_start", "day_end", "now"],
    "proposes": [],
    "never": ["read the owner's calendar, email or files",
              "use the network",
              "move or send anything",
              "keep anything between runs"],
}


def _taught_case(start: str, end: str) -> dict:
    """One complete evaluator input offering exactly ONE slot — what an example's `case` must be."""
    day = start[:10]
    return {"offered_slots": [{"start": start, "end": end}], "duration_minutes": 0,
            "minimum_notice_minutes": 0, "busy": [], "protected": None, "venues": [],
            "day_start": f"{day}T09:00:00+01:00", "day_end": f"{day}T18:00:00+01:00",
            "now": f"{day}T08:00:00+01:00"}


def _kept_clock(parsed: dict) -> str:
    """A clock time inside the working day that the parsed rule KEEPS, or "" when it leaves no room.

    The pair of examples has to straddle the line, so the kept one cannot be a fixed 15:00: for a
    rule with a ceiling of 12:00 that time is refused, the example would say `kept: true` about a
    slot the code refuses, and `teach.example_disagreements` would fail the attempt. Deriving it
    from the same parse the code is built from is what keeps the sentence, the examples and the code
    saying one thing.

    The window is the sample's own working day (09:00–18:00, the window `_taught_case` offers), and
    a whole hour is preferred so the example reads like a meeting somebody would actually propose.
    """
    low = max(9 * 60, _minutes(parsed["floor"])) if parsed["floor"] else 9 * 60
    high = min(18 * 60, _minutes(parsed["ceiling"])) if parsed["ceiling"] else 18 * 60
    whole = [h for h in range(9, 18) if low <= h * 60 < high]
    if whole:
        # The hour nearest the line the rule draws: the first one a floor allows, the last one a
        # ceiling allows. A kept example far from the line shows the owner nothing to check.
        return "%02d:00" % (whole[-1] if parsed["ceiling"] else whole[0])
    if low < high:
        return "%02d:%02d" % divmod(low, 60)
    return ""


def _minutes(hhmm: str) -> int:
    hour, _, minute = str(hhmm).partition(":")
    return int(hour or 0) * 60 + int(minute or 0)


def _refused_clock(parsed: dict, kept: str) -> str:
    """A clock time the parsed rule REFUSES, chosen to be the nearest one to the kept example.

    Nearest, rather than far away, because the two examples side by side are what shows the owner
    where the line falls: "09:30 no, 10:00 yes" is a rule they can check, and "08:00 no, 15:00 yes"
    is a rule they have to take on trust.
    """
    kept_minutes = _minutes(kept)
    if parsed["floor"]:
        return "%02d:%02d" % divmod(max(0, _minutes(parsed["floor"]) - 30), 60)
    if parsed["ceiling"]:
        return "%02d:%02d" % divmod(min(23 * 60 + 45, _minutes(parsed["ceiling"]) + 30), 60)
    return kept


def taught_examples(rule: str, parsed: dict) -> list[dict]:
    """Two labelled cases the owner reads, derived from the SAME reading of the rule the code
    implements — so the sentence, the examples and the code cannot say three different things.

    They are checked against the code before the owner sees them (`teach.example_disagreements`).
    That makes them consistent with the code and says nothing about whether the rule is the one the
    owner meant; the owner reading them is that check, and it is the reason they are on the screen.

    Returns [] for a rule this fixture reads as refusing everything or nothing inside a working day:
    with no pair to show, there is no honest example set, and `example_problems` turns the empty
    list into a named failed attempt rather than a screen showing two cases that say nothing.
    """
    days = parsed["days"]
    banned_day = _WEEK[days[0]] if days else _WEEK[0]
    kept_day = next((d for i, d in enumerate(_WEEK) if i not in days), _WEEK[0])
    kept = _kept_clock(parsed)
    if not kept:
        return []
    if days:
        # The weekday is the rule, so the refused example is a time that would otherwise be fine —
        # and the kept one is the same clock time on a day the rule allows.
        refused_start = f"{banned_day}T{kept}:00+01:00"
        refused_why = f"{banned_day} is a {_weekday_name(banned_day)}, which your rule rules out"
        refused_label = f"{banned_day} at {kept} — the day your rule rules out"
    else:
        refused = _refused_clock(parsed, kept)
        refused_start = f"{banned_day}T{refused}:00+01:00"
        if parsed["floor"]:
            refused_why = f"it starts before {parsed['floor']}, which is the line your rule draws"
            refused_label = f"{banned_day} at {refused} — before {parsed['floor']}"
        else:
            refused_why = f"it starts after {parsed['ceiling']}, which is the line your rule draws"
            refused_label = f"{banned_day} at {refused} — after {parsed['ceiling']}"
    return [
        {"input": refused_label, "kept": False, "why": refused_why,
         "case": _taught_case(refused_start, _plus45(refused_start))},
        {"input": f"{kept_day} at {kept}", "kept": True,
         "why": "nothing in your rule refuses it, and the time is free",
         "case": _taught_case(f"{kept_day}T{kept}:00+01:00", _plus45(f"{kept_day}T{kept}:00+01:00"))},
    ]


def _weekday_name(day: str) -> str:
    import datetime as _dt
    return _dt.date.fromisoformat(day).strftime("%A")


def _plus45(start: str) -> str:
    """The instant 45 minutes after `start`, offset and all, so an example's slot is a real meeting
    length AND a well-formed input: `SkillManifest.validate_inputs` refuses a datetime without an
    offset, so a `HH:MM:SS` here would make every example unreadable at the first check."""
    import datetime as _dt
    return (_dt.datetime.fromisoformat(start) + _dt.timedelta(minutes=45)).isoformat()


def taught_generation_envelope(payload: dict) -> dict:
    """The taught generator's answer, routed on the request's content like every other phase here.

    An unreadable rule answers `{}` — no envelope at all. `learn`'s reader turns that into a named
    failure ("the generator never produced a usable envelope"), which is the truthful report: this
    fixture has no skill for a rule it did not understand.
    """
    rule = str(payload.get("owner_rule") or "")
    parsed = parse_taught_rule(rule)
    if not parsed["understood"]:
        return {}
    inputs = payload.get("inputs_schema") or {}
    outputs = payload.get("outputs_schema") or {}
    if isinstance(payload.get("previous_attempt"), dict):
        code = TAUGHT_MODULE_TEMPLATE % {"rule": rule, "floor": parsed["floor"],
                                         "ceiling": parsed["ceiling"], "days": parsed["days"]}
        tests = TAUGHT_TESTS
    else:
        code, tests = TAUGHT_V1_MODULE, TAUGHT_TESTS
    return {"module_code": code, "tests_code": tests,
            "manifest": {**TAUGHT_MANIFEST, "inputs": inputs, "outputs": outputs},
            "examples": taught_examples(rule, parsed)}


#: Imports the runtime refuses by name. A reviewer that approves code containing one of these has
#: not done its job, and the sample would then be demonstrating a reviewer that does not exist.
_ESCAPES = ("urllib", "socket", "httpx", "requests", "subprocess", "ctypes", "os.environ")


def review_answer(payload: dict) -> dict:
    """The reviewer's verdict, chosen by reading the CODE it was sent.

    Its one job, in the product's words: reject code that reads more than the manifest declares."""
    code = str(payload.get("code") or "")
    hit = [needle for needle in _ESCAPES if needle in code]
    if hit:
        return {"verdict": "reject",
                "reasons": [f"the code uses {', '.join(hit)}, which is outside what this skill "
                            f"declares it may read; it must be a pure function of its inputs",
                            "the runtime already refused it at import time — see the violations "
                            "in the report"]}
    declared = set((payload.get("manifest") or {}).get("reads") or ())
    return {"verdict": "approve",
            "reasons": [f"it computes only from the {len(declared)} inputs it declares",
                        "it proposes no effects",
                        "the held-out cases it never saw pass, which is the evidence that "
                        "matters"]}
