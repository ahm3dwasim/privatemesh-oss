"""The 2026-09-22 real-model defect: a sentence that rejects one time and proposes another.

"Also, 13:00 on Tuesday no longer works for the benefits assessment — move it to 13:45 and
confirm." asks to move TO 13:45. A live Nemotron answer put the rejected 13:00 in `options`
instead, and the engine — which only ever selects among what it is offered — proposed the hour the
sender had just said did not work. `changes.ANSWER_KEYS["extract"]` now has a `rejected_times` key
alongside `options` for exactly this, and `_extract` also filters defensively: even a model that
still puts a rejected time in `options` cannot make the engine propose it.

Reproduced end to end through the sample/fake providers, on the same pinned event, sender and
thread shape as `test_fix_live_venue_kept.py`."""
from __future__ import annotations

import pytest

from privatemesh.decisions import RULE_MODEL
from privatemesh.ports import CalendarEvent

from fakes_v2 import Harness, at, change_email

LOCATION = "Northgate Clinic, Room 4"
#: The corpus's placeholder correspondent (`fakes_v2.change_email`'s own default), not the live
#: run's sender: the person that run wrote to is a real one, and their name and address were
#: replaced here with a fictional pair before this repository went public. The thread SHAPE is the
#: live one — same event, same times, same words — so what these tests measure is unchanged.
SENDER_ADDRESS = "alex@example.com"
SENDER = f"Alex Doe <{SENDER_ADDRESS}>"


def _event() -> CalendarEvent:
    return CalendarEvent(id="ev1", etag="etag-1", summary="Benefits assessment (Rosa)",
                         start=at("13:00"), end=at("13:45"), timezone="Europe/London",
                         location=LOCATION, attendees=(SENDER_ADDRESS,))


def _thread_message():
    return change_email(
        "m1", sender=SENDER, subject="Action needed: confirm Rosa's benefits record",
        body="Hi, also 13:00 on Tuesday no longer works for the benefits assessment — move it to "
             "13:45 and confirm. Thanks, Alex")


def _harness(*, extract_options, extract_rejected):
    script = {
        "classify": {"is_meeting_change": True, "reason": "asks to move the appointment"},
        "extract": {"options": extract_options, "rejected_times": extract_rejected,
                    "request_excerpt": "Move the benefits assessment to 13:45."},
        "draft": {"template_body": "Hi,", "fragments": [{"source": "time",
                                                         "text": "13:45-14:30 works for me."}]},
    }
    message = _thread_message()
    return Harness(messages=[message], threads={"t1": [message]}, events=[_event()], search={},
                   script=script)


def test_a_correct_extraction_of_both_times_proposes_the_time_moved_to_not_rejected():
    """(a) The realistic failure shape: the extractor names `rejected_times` correctly but still
    puts BOTH times in `options`, 13:00 (the one it just called rejected) ahead of 13:45. Without
    the filter, the engine takes the first fitting option and proposes the rejected 13:00 — this is
    the exact live defect the fix exists for. With the filter, 13:00 is dropped before the fit
    check ever sees it and the proposal is 13:45-14:30 on the pinned benefits-assessment event,
    with a decision note recording the drop."""
    h = _harness(extract_options=[{"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"},
                                  {"start": at("13:45"), "end": at("14:30"), "phrase": "13:45"}],
                 extract_rejected=[{"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"}])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "proposal_ready", change.error
    assert change.proposal["start"] == at("13:45")
    assert change.proposal["end"] == at("14:30")
    assert change.event_pinned == "ev1"
    notes = h.engine.decisions_for(change.id)
    assert any("no longer works" in n["subject"] for n in notes), notes


def test_a_model_that_puts_the_rejected_time_in_options_anyway_is_not_proposed_it():
    """(b) A scripted model behaving exactly like the live defect: the ONLY entry in `options` is
    the time the sender said no longer works, and it is also named in `rejected_times`. The engine
    must not invent a nearby time either — it drops the option and asks the owner rather than
    proposing 13:00."""
    h = _harness(extract_options=[{"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"}],
                 extract_rejected=[{"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"}])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "no_safe_fit", change.proposal
    assert change.proposal is None
    assert change.plan_state == "needs_you"


# ================================================================================================
# The re-audit's Low: a field of the extract answer carrying the WRONG TYPE.
#
# `_validated` asks whether a key is PRESENT, and `options: 5` and `rejected_times: 5` are both
# present. What the two loops below it did with that was iterate the number — a bare `TypeError`
# out of `prepare`, which in the product is a 500 to the owner who clicked and no proposal either
# way. A malformed field now REFUSES (the engine's existing `no_safe_fit`: nothing proposed, the
# owner asked), and the shapes below are the ways one arrives: a number, a boolean, and — the one
# shape that IS readable, as exactly one rejected time — a bare object.
# ================================================================================================


def test_options_that_are_not_a_list_refuse_the_change_instead_of_raising():
    """`options: 5` is not "the sender offered no time" — that is a legitimate-looking negative
    read off a key the consumer could not use, which is `prompt_contract`'s whole subject — and it
    is not a crash either. It is an answer the engine cannot read, so it refuses."""
    h = _harness(extract_options=5, extract_rejected=[])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "no_safe_fit", change.proposal
    assert change.proposal is None
    assert change.plan_state == "needs_you"


def test_rejected_times_that_are_not_a_list_refuse_the_change_instead_of_raising():
    """The optional field, and the reason its malformation is a REFUSAL rather than an empty list:
    read as empty, the filter below stops running and the engine is free to propose the very time
    the sender said no longer works — the live defect this key was added for. So the fitted 13:45
    offered here is deliberately NOT proposed: an answer whose rejected times cannot be read is an
    answer whose times are unknown, not one that rejected nothing."""
    h = _harness(extract_options=[{"start": at("13:45"), "end": at("14:30"), "phrase": "13:45"}],
                 extract_rejected=5)
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "no_safe_fit", change.proposal
    assert change.proposal is None
    assert change.plan_state == "needs_you"


def test_a_rejected_times_of_true_refuses_the_change_instead_of_raising():
    """The same field, the other wrong type a real model reached for. `True` is truthy, so the old
    `or []` handed it straight to the loop."""
    h = _harness(extract_options=[{"start": at("13:45"), "end": at("14:30"), "phrase": "13:45"}],
                 extract_rejected=True)
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "no_safe_fit", change.proposal
    assert change.proposal is None
    assert change.plan_state == "needs_you"


@pytest.mark.parametrize("field", ("options", "rejected_times"))
@pytest.mark.parametrize("value", (False, 0, ""), ids=("false", "zero", "empty-string"))
def test_a_falsy_wrong_type_refuses_the_change_instead_of_reading_as_absent(field, value):
    """The three FALSY wrong types, which are the ones a `value or []`-shaped guard lets through.

    Truthiness is the wrong question and `True` is not the interesting case: `rejected_times: 5`
    and `rejected_times: True` are both caught by a glance, but `False`, `0` and `""` are exactly
    what an "is it empty" test reads as ABSENT — and absent is the one answer this field must never
    be given, because empty switches the filter off and hands the engine back the time the sender
    said no longer works. The fit runs on the same code as the number and the boolean do — the
    field is checked for being a list, not for being truthy — so the 13:45 offered here is refused
    rather than proposed, and refused identically for every falsy spelling.

    Parametrized over BOTH fields because it is one class of defect, which is what the comment block
    above says: `options: ""` reads as "the sender offered no time" and `rejected_times: ""` reads
    as "nothing was rejected", and both are a key the engine could not use being reported as a
    legitimate negative."""
    offered = {"options": [{"start": at("13:45"), "end": at("14:30"), "phrase": "13:45"}],
               "rejected_times": []}
    offered[field] = value
    h = _harness(extract_options=offered["options"], extract_rejected=offered["rejected_times"])
    change = h.engine.prepare(h.engine.detect(None)[0].id)

    assert change.stage == "no_safe_fit", (field, value, change.proposal)
    assert change.proposal is None
    assert change.plan_state == "needs_you"
    notes = [n for n in h.engine.decisions_for(change.id) if "could not be read" in n["subject"]]
    assert notes, h.engine.decisions_for(change.id)


def test_the_falsy_control_an_empty_rejected_times_still_proposes_the_offered_time():
    """The control for the rule above, and the reason it is not satisfied by refusing everything:
    `rejected_times: []` is the legitimate answer meaning "nothing was rejected", and the very same
    answer that the falsy cases are refused for must still be READ. If this went red alongside them,
    the parametrized test above would be measuring "the engine refuses" rather than "the engine
    refuses a value it could not read"."""
    h = _harness(extract_options=[{"start": at("13:45"), "end": at("14:30"), "phrase": "13:45"}],
                 extract_rejected=[])
    change = h.engine.prepare(h.engine.detect(None)[0].id)

    assert change.stage == "proposal_ready", change.error
    assert change.proposal["start"] == at("13:45")
    assert change.proposal["end"] == at("14:30")


def test_a_single_rejected_time_sent_as_an_object_is_read_rather_than_ignored():
    """The re-audit's nit: one object instead of a list of one. Iterating a dict yields its KEY
    strings, so every lookup below failed, the filter did nothing, and the 13:00 the sender had
    just said no longer works was proposed anyway — the live defect, reopened by a shape. Wrapped,
    it is one rejected time like any other, and the option it names is dropped."""
    h = _harness(extract_options=[{"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"}],
                 extract_rejected={"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"})
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    assert change.stage == "no_safe_fit", change.proposal
    assert change.proposal is None


def test_the_refusal_of_an_unreadable_answer_is_recorded_as_a_decision():
    """A refusal nobody can find afterwards is a silent one. The row names the field the engine
    could not read in its detail — never the answer's text, which is the owner's mail — and the
    harness's decision log is deliberately built unbound to a gate, so it withholds every detail;
    what this asserts is the half the engine writes: the row, its verdict and its rule."""
    h = _harness(extract_options=5, extract_rejected=[])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    notes = [n for n in h.engine.decisions_for(change.id) if "could not be read" in n["subject"]]
    assert notes, h.engine.decisions_for(change.id)
    assert notes[0]["verdict"] == "refused", notes[0]
    assert notes[0]["rule"] == RULE_MODEL, notes[0]


def test_the_dropped_option_is_recorded_as_a_decision():
    """The filtering is not silent: a note is recorded when the model's `options` disagreed with
    its own `rejected_times`, so the record can show the owner what happened."""
    h = _harness(extract_options=[{"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"}],
                 extract_rejected=[{"start": at("13:00"), "end": at("13:45"), "phrase": "13:00"}])
    change = h.engine.prepare(h.engine.detect(None)[0].id)
    notes = h.engine.decisions_for(change.id)
    assert any("no longer works" in n["subject"] for n in notes), notes
