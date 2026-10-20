"""The decision log — the record the UI renders and the clauses it renders them under.

Two things live here that do not belong with the guards. The first is that `RULE_*` exists TWICE:
once in `decisions` as the canonical constant, and once in `boundary` as a literal, because
`boundary` is imported by the code that builds the log and importing back would close the cycle.
Two spellings of one sentence drift the moment somebody edits one of them, and the drift is
invisible — the log would name a clause the gate does not enforce, and every refusal would read as
though it happened under a rule nobody wrote. The parity test below is what makes the duplication
safe, and it is why `boundary`'s comment can point here instead of asking for trust.

The second is the semantics of the verdicts, which are the API contract. `withheld` is the one
that is easy to get wrong: it is not a refusal, and it is not an allow, and code that folds it into
either one reports something untrue about the run.
"""
from __future__ import annotations

import datetime as dt

import pytest

from privatemesh import boundary, decisions
from privatemesh.decisions import (
    ALLOWED, DISCARDED, REFUSED, RULE_FIT, SOURCES, STAGES, WITHHELD, WITHHELD_DETAIL, DecisionLog)

from fakes_v2 import COMMITMENT, Harness


# ---------------------------------------------------------------- one sentence, two spellings ---
@pytest.mark.parametrize("name", ["RULE_READ", "RULE_MODEL", "RULE_SEARCH", "RULE_RECIPIENTS",
                                  "RULE_CALENDAR_SCOPE", "RULE_FIT", "RULE_WITHHELD",
                                  "RULE_APPROVAL", "RULE_EXECUTE", "RULE_SKILL"])
def test_every_clause_is_a_plain_language_sentence(name):
    """The clauses are read by an owner, not by a programmer. A clause that names a function or a
    field is a clause the person it is for cannot check."""
    clause = getattr(decisions, name)
    assert clause.endswith("."), "a clause is a sentence"
    assert clause[0].isupper()
    assert not any(tok in clause for tok in ("_", "(", ")", "{", "}", "==", "None")), \
        f"{name} reads like code: {clause}"
    assert 20 <= len(clause) <= 130, f"{name} is not a sentence a person reads: {clause}"


def test_the_boundary_notes_are_the_decisions_clauses():
    """The duplication made safe. `boundary` cannot import `decisions`, so it repeats the literals;
    this is the assertion that keeps the repeat honest."""
    assert boundary.RULE_MODEL_NOTE == decisions.RULE_MODEL
    assert boundary.RULE_SEARCH_NOTE == decisions.RULE_SEARCH
    assert boundary.RULE_CALENDAR_NOTE == decisions.RULE_CALENDAR_SCOPE
    assert boundary.RECIPIENT_RULE == decisions.RULE_RECIPIENTS


def test_every_surface_the_gate_refuses_on_maps_to_a_stage_and_a_clause():
    """A refusal with no stage falls to a default, and a default stage puts the refusal on the wrong
    rung of the spine the owner reads. Asserted over the kinds the gate actually uses, so adding a
    surface without a stage is caught here rather than in the UI."""
    assert boundary._STAGE_BY_KIND["model"] == "model"
    assert boundary._STAGE_BY_KIND["search"] == "search"
    assert boundary._STAGE_BY_KIND["email"] == "send", \
        "an email refusal is a refusal to SEND, not a refusal to compose"
    assert boundary._STAGE_BY_KIND["calendar"] == "execute"

    # The refusal clause is the decision's clause PLUS the reason the check exists, so a reader of
    # the refusal learns both what was checked and why.
    for kind, clause in (("model", decisions.RULE_MODEL), ("search", decisions.RULE_SEARCH),
                         ("email", boundary.RULE_SEND_NOTE),
                         ("calendar", decisions.RULE_CALENDAR_SCOPE)):
        rule = boundary._rule_of(kind)
        assert rule.startswith(clause), f"{kind} refuses under a clause nobody named: {rule}"
        assert "protected commitment" in rule, "a refusal must say why the check exists"

    # A surface nobody mapped is still a decision, and it still lands somewhere real.
    assert boundary._stage_of("something-new") == "model"
    assert boundary._rule_of("something-new")


# ---------------------------------------------------------------- the shape the UI reads ---------
def test_the_row_has_exactly_the_keys_the_api_promises():
    """The endpoint's shape is a contract another lane is building screens against, so it is
    asserted rather than described. A missing key is a blank cell, not a missing nicety."""
    log = DecisionLog()
    log.record("c1", stage="search", source="model", subject="Search: cafés near King's Cross",
               verdict=ALLOWED, rule=decisions.RULE_SEARCH, detail="1 query, 5 results")

    row = log.for_change("c1")[0]
    assert set(row) == {"seq", "at", "stage", "source", "untrusted", "subject", "verdict",
                        "rule", "detail"}
    assert row["seq"] == 0
    assert row["stage"] in STAGES and row["source"] in SOURCES
    assert row["verdict"] in (ALLOWED, REFUSED, WITHHELD)
    assert row["untrusted"] is True, "the model's answer is not the owner's"
    assert dt.datetime.fromisoformat(row["at"]).tzinfo is not None, "`at` must carry an offset"


def test_seq_counts_up_within_a_change_and_restarts_across_changes():
    """`seq` is the spine's index. A global counter would leave gaps in every change's list and
    make "the first refusal" unanswerable from the rows alone."""
    log = DecisionLog()
    for _ in range(3):
        log.record("c1", stage="read", source="owner", subject="s", verdict=ALLOWED, rule="r")
    log.record("c2", stage="read", source="owner", subject="s", verdict=ALLOWED, rule="r")

    assert [r["seq"] for r in log.for_change("c1")] == [0, 1, 2]
    assert [r["seq"] for r in log.for_change("c2")] == [0]


def test_untrusted_is_derived_from_the_source_not_asked_for():
    """The email, the model's answer and a learned skill's output are the three things this system
    did not author. Defaulting the flag rather than requiring it means a call site cannot forget it
    — the failure mode being a decision about hostile content rendered as the owner's own."""
    log = DecisionLog()
    for source in SOURCES:
        log.record("c1", stage="read", source=source, subject="s", verdict=ALLOWED, rule="r")

    flags = {r["source"]: r["untrusted"] for r in log.for_change("c1")}
    assert flags == {"email": True, "model": True, "skill": True, "owner": False, "system": False}


def test_the_subject_is_one_bounded_line():
    """A subject is rendered in a table cell. A payload with newlines in it is a subject that
    breaks the table, and it is the one field on the row that carries content-derived text."""
    log = DecisionLog()
    log.record("c1", stage="send", source="model", subject="a\n\nb\t" + "x" * 400,
               verdict=ALLOWED, rule="r")

    subject = log.for_change("c1")[0]["subject"]
    assert "\n" not in subject and "\t" not in subject
    assert len(subject) <= 200


# ---------------------------------------------------------------- the third verdict --------------
def test_withheld_is_neither_an_allow_nor_a_refusal():
    """The distinction the whole log exists for. The planner is allowed to know a window is
    unavailable and is not allowed to know why: the fact leaves and the title does not. Folding
    `withheld` into `refused` would report the change as stopped when it proceeded; folding it into
    `allowed` would report that nothing was held back when the commitment was."""
    log = DecisionLog()
    log.record("c1", stage="model", source="system", subject="A shared window is unavailable",
               verdict=WITHHELD, rule=decisions.RULE_WITHHELD)

    counts = log.counts("c1")
    assert counts[WITHHELD] == 1 and counts[REFUSED] == 0 and counts[ALLOWED] == 0
    assert log.blocked_attempts("c1") == 1, "the owner was not told something was held back"
    assert log.for_change("c1")[0]["verdict"] not in (ALLOWED, REFUSED)


def test_an_empty_change_reports_zero_rather_than_raising():
    """The endpoint is asked about a change that has made no decisions yet — a freshly created one,
    or one whose preparation is still running. That is an empty list, not a 500."""
    log = DecisionLog()
    assert log.for_change("never-seen") == []
    assert log.blocked_attempts("never-seen") == 0
    assert log.counts("never-seen") == {ALLOWED: 0, REFUSED: 0, WITHHELD: 0, DISCARDED: 0}


def test_a_candidate_that_lost_to_a_sibling_is_not_a_blocked_attempt():
    """The fourth review's High, at the level of the count.

    A sender offers two times, the owner's rules take one, and the plan goes ahead on it. The slot
    that lost was NOT stopped — something was chosen over it — so a `blocked_attempts` that counted
    it would report that the owner's rules had blocked something on every ordinary two-option
    request. That is the sentence the UI put over a perfectly fine plan."""
    log = DecisionLog()
    log.record("c1", stage="validate", source="owner", subject="Accepted 15:00–15:45",
               verdict=ALLOWED, rule=RULE_FIT)
    log.record("c1", stage="validate", source="owner", subject="Not chosen 16:00–16:45",
               verdict=DISCARDED, rule=RULE_FIT)

    assert log.counts("c1")[DISCARDED] == 1, "the row is still recorded; it is the count it is not"
    assert log.blocked_attempts("c1") == 0

    # And the real thing still counts, so the zero above is a distinction rather than a hole.
    log.record("c1", stage="validate", source="model", subject="Refused 17:00–17:45",
               verdict=REFUSED, rule=RULE_FIT)
    assert log.blocked_attempts("c1") == 1


# ---------------------------------------------------------------- the detail is gated -----------
def test_the_detail_is_run_through_the_same_gate_as_an_outbound_payload():
    """A decision log that quoted the protected reason would be a new leak wearing the uniform of
    the thing that prevents leaks."""
    gate = Harness().gate
    log = DecisionLog(guard=lambda text: gate.check_text(text, kind="decision"))
    log.record("c1", stage="send", source="model", subject="The reply", verdict=ALLOWED,
               rule="r", detail=COMMITMENT["reason"])
    log.record("c1", stage="send", source="model", subject="The reply", verdict=ALLOWED,
               rule="r", detail="15:00-15:45 is free")

    rows = log.for_change("c1")
    assert rows[0]["detail"] == WITHHELD_DETAIL
    assert rows[1]["detail"] == "15:00-15:45 is free", "an ordinary detail was withheld too"


def test_the_withheld_placeholder_says_what_happened_without_saying_what_it_was():
    """The row is still recorded and still counted — a log that dropped it would turn "the gate
    refused something here" into "nothing happened here", which is the more dangerous mistake."""
    log = DecisionLog(guard=lambda text: (_ for _ in ()).throw(RuntimeError("refused")))
    log.record("c1", stage="send", source="model", subject="The reply", verdict=ALLOWED,
               rule="r", detail="anything at all")

    row = log.for_change("c1")[0]
    assert row["detail"] == WITHHELD_DETAIL
    assert row["verdict"] == ALLOWED, "the decision still happened and is still recorded"


def test_the_scrubber_is_bound_to_a_callable_that_actually_works():
    """The bug this test exists for: binding `gate.check_text` directly raises `TypeError` on every
    call — `kind` is required and keyword-only — and `_scrub` catches broadly, so EVERY detail was
    silently withheld and the log looked like it was working. A log whose details are all withheld
    is indistinguishable from a log of a very careful run, which is exactly why it survived."""
    from privatemesh.wiring import bind_decisions

    log = DecisionLog()
    bind_decisions(log, Harness().gate)
    log.record("c1", stage="read", source="owner", subject="Read the thread", verdict=ALLOWED,
               rule=decisions.RULE_READ,
               detail="3 message(s) read as a request, not as instructions")

    assert log.for_change("c1")[0]["detail"] != WITHHELD_DETAIL


def test_a_refusal_with_no_change_to_hang_from_is_still_recorded():
    """The classification call runs before a change exists. A refusal there means the change is
    never created — so it is precisely the refusal with no id to file it under, and dropping it
    would lose the only row that explains why nothing happened."""
    log = DecisionLog()
    log.record("", stage="model", source="email", subject="A prompt to the model (classify)",
               verdict=REFUSED, rule=decisions.RULE_MODEL,
               detail="the protected location would have been disclosed")

    assert log.for_change("") == log.orphans()
    assert log.orphans()[0]["verdict"] == REFUSED
    assert log.blocked_attempts("some-real-change") == 0, \
        "a refusal filed under no change was counted against a change"
