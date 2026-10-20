"""The standing limits: the owner's three presets, and the clauses that bound the middle one.

Two questions are asked here and they are different questions.

*Does the STORE behave?* The default is `ask_always`, a mode survives a restart, and an unknown mode
is refused rather than coerced. Those are the promises an owner is relying on when they stop reading
every change.

*Does the ENFORCEMENT hold?* A benign change runs with no approval and cites all six clauses; a
change that leaves the limit falls back to asking with the failed clause named; `off` prepares and
never runs. Every one of these is checked by trusted code against the world, and the tests below
check the trusted code rather than a model's opinion of it.

The clauses are a fixed list, so these tests can name them. `limits.clause("3").text` is the same
string the UI shows and the same string the decision row carries, which is why the assertions
compare against `clause(...).text` rather than against a literal copied in here.
"""
from __future__ import annotations

import pytest

from privatemesh import limits
from privatemesh.changes import READY
from privatemesh.limits import ASK_ALWAYS, LimitStore, UnknownMode, WITHIN_LIMITS

from fakes_v2 import Harness, at, busy, invite

#: A reply body that IS the render of its fragments — which is what clause 4 asks for. The source
#: has to be one the outbound allowlist names, so `template` rather than something invented.
REPLY = "Hi Alex, 15:00-15:45 works for me."
FRAGMENTS = [{"source": "template", "text": REPLY}]


# ------------------------------------------------------------------ the store -------------------
def test_a_build_that_has_never_chosen_is_ask_always():
    """The default, and the only mode an existing install gets. A standing yes is something an
    owner grants; a fresh column must not grant one on their behalf."""
    store = LimitStore()
    assert store.mode() == ASK_ALWAYS
    assert store.state()["mode"] == ASK_ALWAYS


def test_never_chosen_reads_as_never_chosen_not_as_now():
    """`updated_at` is `None` until the owner sets a mode. Stamping the default with the moment the
    process opened the file would make the default look like a decision the owner made."""
    assert LimitStore().state()["updated_at"] is None


def test_the_catalogue_is_the_clauses_in_order():
    """Seven since the care circle. The count is asserted rather than derived from `CLAUSES`,
    because a catalogue test that reads the thing it is testing cannot notice a clause being
    dropped — and the id list is what the UI numbers, so it is the list that has to be pinned."""
    state = LimitStore().state()
    assert [c["id"] for c in state["clauses"]] == ["1", "2", "3", "4", "5", "6", "7"]
    for row in state["clauses"]:
        assert row["text"] == limits.clause(row["id"]).text
        assert row["cannot"], "a permission an owner cannot see the edges of is not judgeable"


def test_an_unknown_mode_is_refused_rather_than_defaulted():
    """Refused, not coerced. A typo that quietly becomes `within_limits` is the dangerous direction,
    and a typo that quietly becomes `ask_always` reads as a setting that did not save."""
    store = LimitStore()
    with pytest.raises(UnknownMode):
        store.set_mode("within_limit")
    assert store.mode() == ASK_ALWAYS, "a refused mode must not have been written"


def test_a_mode_from_a_newer_build_reads_as_the_default():
    """An unreadable permission is not a licence to act. The safe direction is the one that asks."""
    store = LimitStore()
    store._conn.execute("INSERT OR REPLACE INTO limits(key, value) VALUES('mode', 'yolo')")
    store._conn.commit()
    assert store.mode() == ASK_ALWAYS


def test_the_mode_survives_a_restart(tmp_path):
    """The whole difference between a standing answer and a per-session accident."""
    path = str(tmp_path / "limits.db")
    LimitStore(path=path).set_mode(WITHIN_LIMITS)
    assert LimitStore(path=path).mode() == WITHIN_LIMITS
    assert LimitStore(path=path).state()["updated_at"] is not None


# ------------------------------------------------------------------ the API surface -------------
def test_get_limits_serves_the_contract_shape():
    body = _harness().v2().limits_state(presented=None)
    assert set(body) == {"mode", "clauses", "updated_at"}
    assert body["mode"] == ASK_ALWAYS
    assert body["updated_at"] is None
    assert [c["id"] for c in body["clauses"]] == ["1", "2", "3", "4", "5", "6", "7"]


def test_limits_are_owner_only():
    """Joining the enumerated owner-only routes: the standing answer is the owner's, and a shared
    screen in front of the product is not the owner."""
    from privatemesh.api_v2 import V2Error

    service = _harness().v2(authenticated_owner=False, owner_token="secret")
    with pytest.raises(V2Error) as caught:
        service.limits_state(presented=None)
    assert caught.value.status_code == 403
    with pytest.raises(V2Error):
        service.set_limits(WITHIN_LIMITS, presented=None)


def test_an_unknown_mode_is_refused_over_the_api_too():
    from privatemesh.api_v2 import V2Error

    service = _harness().v2()
    with pytest.raises(V2Error) as caught:
        service.set_limits("yolo", presented=None)
    assert caught.value.status_code == 422
    assert caught.value.code == "unknown_limits_mode"
    assert service.limits.mode() == ASK_ALWAYS

    assert service.set_limits(WITHIN_LIMITS, presented=None)["mode"] == WITHIN_LIMITS
    assert service.limits.mode() == WITHIN_LIMITS


# ------------------------------------------------------------------ enforcement -----------------
def _adopt(harness: Harness, change):
    """Put a change where the engine will find it.

    `Harness` registers its fixture change with the RESOLVER, which is what the send path reads its
    recipient pin from; the engine keeps its own map. A test that drives `prepare` needs the change
    in the engine, and this is the one line that says so."""
    harness.engine._changes[change.id] = change
    return change


def _decisions(harness: Harness):
    """Attach a real decision log to the harness's engine.

    `Harness` builds the engine without one, which is a legitimate build — `_note` is a documented
    no-op then. But the clauses ARE decision rows, so every test below that asserts on one needs a
    log, and a log whose scrubber is not bound would withhold every detail and look like it was
    working."""
    from privatemesh.decisions import DecisionLog
    from privatemesh.wiring import bind_decisions

    log = DecisionLog()
    bind_decisions(log, harness.gate)
    harness.engine.decisions = log
    return log


def _harness() -> Harness:
    """A harness whose decisions are actually recorded. See `_decisions`."""
    harness = Harness()
    _decisions(harness)
    return harness


def _prepared(harness: Harness, *, fragments=None, template="", **over):
    """A change in `proposal_ready` with a pending bundle, built the way `prepare` builds one.

    Hand-built rather than driven through `detect`/`prepare` because several of these tests are
    about bundles the ENGINE would never have proposed — a reply with two recipients is the
    example, and it is exactly the case the clauses exist to catch. A fixture that could only
    produce bundles the planner already approved of could not test the boundary at all.
    """
    change = _adopt(harness, harness.change_resolver.registered["c1"])
    change.stage = READY
    change.event_pinned = "ev1"
    change.reply_fragments = list(FRAGMENTS if fragments is None else fragments)
    change.reply_template = template
    cs = harness.pending_changeset(**over)
    change.change_set_id = cs.id
    change.version = cs.version
    return change, cs


def _benign(harness: Harness, **over):
    """A bundle inside every clause: the pinned meeting, a slot the validator accepts, one recipient
    who is the pinned sender, one reply that is the render of its own fragments, one edit.

    `rfc822_message_id` is here because the engine's `_prepare` always writes it — `ChangeEngine.
    _rfc822` reads it off the pinned thread — and the port now requires it to equal BOTH the
    approved value and the provider's own copy of that message, since `In-Reply-To` and `References`
    are built from it. `<m1@...>` is what `message("m1")` serves, so this is the value the real
    producer would have written; a bundle without one is not a simpler fixture, it is one that
    models a producer that does not exist."""
    over.setdefault("email_args", {
        "to": ["alex@example.com"], "subject": "Re: Our usual cafe has closed", "body": REPLY,
        "in_reply_to": "m1", "thread_id": "t1", "change_id": "c1",
        "rfc822_message_id": "<m1@example.com>",
        "idempotency_key": "idem-c1-v1"})
    return _prepared(harness, **over)


def _standing(harness: Harness, mode: str):
    """A service over the harness's OWN limits store — the same object its executor re-checks a
    grant against. A second store here would authorize under one mode and re-check against another,
    which is the split the grant exists to close and would make these tests prove nothing."""
    service = harness.v2()
    service.limits.set_mode(mode)
    return service


def _clause_rows(harness: Harness, change_id: str) -> list[dict]:
    rules = {c.text for c in limits.CLAUSES}
    return [r for r in harness.engine.decisions_for(change_id) if r["rule"] in rules]


def test_within_limits_runs_a_benign_change_without_asking():
    """The middle preset's whole promise: a change inside the limit runs, and the owner is told
    afterwards rather than asked first."""
    harness = _harness()
    change, _ = _benign(harness)
    service = _standing(harness, WITHIN_LIMITS)

    body = service.prepare(change.id)

    assert body["stage"] == "done"
    assert body["executed_by"] == "limit"
    assert harness.calendar.get_event("ev1").start == at("15:00")
    assert [tuple(m["to"]) for m in harness.mail.sent] == [("alex@example.com",)]


def test_the_limit_run_cites_every_clause_in_the_decision_log():
    """Every verdict, not one. The owner's standing yes is a fixed list of sentences, and a run that
    recorded only the clause it happened to check would be a run they cannot audit.

    Clause 7 is cited here on a SCHEDULING bundle, where it passes by saying the plan sends nothing
    to the care circle. That is the honest reading of a conditional clause and it is asserted
    rather than skipped: a clause that silently vanished from the log whenever it did not apply
    would be indistinguishable from one that had been deleted."""
    harness = _harness()
    change, _ = _benign(harness)
    service = _standing(harness, WITHIN_LIMITS)
    service.prepare(change.id)

    by_rule = {r["rule"]: r for r in _clause_rows(harness, change.id)}
    for clause in limits.CLAUSES:
        assert clause.text in by_rule, f"clause {clause.id} was never recorded"
        assert by_rule[clause.text]["verdict"] == limits.ALLOWED, clause.id
        assert by_rule[clause.text]["source"] == "owner", "a clause is the owner's own sentence"


def test_a_reply_that_names_a_second_recipient_falls_back_to_asking():
    """Out of limit is not a refusal of the change: it is the fallback to `ask_always`, with the
    clause that failed named in the owner's own words."""
    harness = _harness()
    change, _ = _benign(harness, email_args={
        "to": ["alex@example.com", "dave@example.com"],
        "subject": "Re: Our usual cafe has closed", "body": REPLY,
        "in_reply_to": "m1", "thread_id": "t1", "change_id": "c1", "idempotency_key": "idem-c1-v1"})
    service = _standing(harness, WITHIN_LIMITS)

    body = service.prepare(change.id)

    assert body["stage"] == "proposal_ready", "an out-of-limit change must wait for the owner"
    assert body["executed_by"] is None
    assert harness.mail.sent == []
    failed = [r for r in _clause_rows(harness, change.id)
              if r["verdict"] == limits.REFUSED and r["rule"] == limits.clause("3").text]
    assert failed, "the clause that failed has to be readable in the log"
    assert "2 recipients" in failed[0]["detail"]


def test_a_change_with_no_bundle_records_no_clause_rows():
    """Nothing to review is not a review. Six rows about a plan that does not exist would be noise
    in the owner's log and would inflate the change's blocked attempts."""
    harness = _harness()
    change = _adopt(harness, harness.change_resolver.registered["c1"])
    change.stage = READY
    change.event_pinned = "ev1"
    service = _standing(harness, WITHIN_LIMITS)

    body = service.prepare(change.id)

    assert body["stage"] == "proposal_ready", "no bundle, so nothing for the limits to run"
    assert _clause_rows(harness, change.id) == []
    assert harness.engine.decisions_for(change.id) == [], "and no rows at all, not six empty ones"


def test_ask_always_does_not_run_and_does_not_consult_the_clauses():
    """The default is not the middle preset with a smaller number. Under `ask_always` the clauses
    are not consulted at all: there is no standing yes to check a plan against."""
    harness = _harness()
    change, _ = _benign(harness)
    service = _standing(harness, ASK_ALWAYS)

    body = service.prepare(change.id)

    assert body["stage"] == "proposal_ready"
    assert body["executed_by"] is None
    assert harness.mail.sent == []
    assert _clause_rows(harness, change.id) == []
    # And the owner path still works from there, which is what the fallback falls back TO.
    approved = service.approve(change.id, body["version"])
    assert approved["executed_by"] == "owner"
    assert [tuple(m["to"]) for m in harness.mail.sent] == [("alex@example.com",)]


def test_off_prepares_but_refuses_to_run_even_when_approved():
    """`off` is not `ask_always`. The routine still watches and prepares; nothing executes, and an
    approval presented anyway is refused LOUDLY rather than silently discarded — a preset that
    swallowed the owner's yes would leave the review screen saying "approved" over a meeting that
    never moved."""
    from privatemesh.api_v2 import V2Error

    harness = _harness()
    change, _ = _benign(harness)
    service = _standing(harness, limits.OFF)

    body = service.prepare(change.id)
    assert body["stage"] == "proposal_ready", "off still prepares; that is its whole behaviour"
    assert body["executed_by"] is None
    assert harness.mail.sent == []

    with pytest.raises(V2Error) as caught:
        service.approve(change.id, body["version"])
    assert caught.value.status_code == 409
    assert caught.value.code == "limits_off"
    assert caught.value.detail == limits.OFF_MEANS
    assert harness.mail.sent == [], "a refused approval must have run nothing"
    assert harness.calendar.get_event("ev1").start == at("15:00"), "and moved nothing"


def test_a_clause_that_fails_on_a_reread_stops_the_run():
    """The clauses are re-derived from the world at the moment execution would happen. A day that
    changed between the plan and the run is a day the plan no longer fits, and the limit does not
    cover it — which is the difference between checking the PLAN and checking the WORLD."""
    harness = _harness()
    change, _ = _benign(harness)
    service = _standing(harness, WITHIN_LIMITS)
    # 15:00 is taken by the time the limits are consulted. Deliberately NOT 15:00-15:45: an interval
    # that IS the meeting's own block is dropped from the check (a meeting is never booked against
    # itself), so an identical window would prove nothing.
    harness.calendar.busy.append(busy("14:45", "15:30"))

    body = service.prepare(change.id)

    assert body["stage"] == "proposal_ready"
    assert harness.mail.sent == []
    refused = [r for r in _clause_rows(harness, change.id) if r["verdict"] == limits.REFUSED]
    assert refused, "a clause refused and the owner has to be able to see which"
    assert any(r["rule"] == limits.clause("2").text for r in refused), \
        [(r["rule"], r["detail"]) for r in refused]
    assert all(r["detail"] for r in refused), "a refusal without a reason is not readable"


def test_moving_the_mode_does_not_retroactively_run_a_waiting_change():
    """A change already waiting for the owner is not a change the limit may act on by itself: the
    owner is looking at it, and it is theirs to answer. Flipping the preset is not an approval."""
    harness = _harness()
    change, _ = _benign(harness)
    service = _standing(harness, ASK_ALWAYS)
    service.prepare(change.id)
    assert harness.mail.sent == []

    service.limits.set_mode(WITHIN_LIMITS)
    assert service.change(change.id)["stage"] == "proposal_ready"
    assert harness.mail.sent == [], "setting a preset is not approving the plan on the screen"


# ------------------------------------------------------------------ the clauses, one at a time --
def test_clause_one_refuses_a_plan_that_names_another_meeting():
    harness = _harness()
    change, _ = _benign(harness)
    change.event_pinned = "ev-other"
    verdict = harness.engine.limit_review(change.id).verdicts[0]
    assert verdict.refused
    assert "this change was raised from" in verdict.detail


def test_clause_one_refuses_a_move_to_another_day():
    harness = _harness()
    change, _ = _benign(harness, calendar_args={
        **Harness.calendar_args(harness),
        "start": at("15:00", "2026-09-20"), "end": at("15:45", "2026-09-20")})
    verdict = harness.engine.limit_review(change.id).verdicts[0]
    assert verdict.refused
    assert "2026-09-20" in verdict.detail


def test_clause_two_refuses_a_slot_the_validator_would_not_take():
    harness = _harness()
    change, _ = _benign(harness)
    harness.calendar.busy.append(busy("15:30", "16:00"))
    verdict = harness.engine.limit_review(change.id).verdicts[1]
    assert verdict.refused and verdict.detail


def test_clause_three_refuses_a_cc():
    harness = _harness()
    change, _ = _benign(harness, email_args={
        "to": ["alex@example.com"], "cc": ["dave@example.com"],
        "subject": "Re: Our usual cafe has closed", "body": REPLY,
        "in_reply_to": "m1", "thread_id": "t1", "change_id": "c1", "idempotency_key": "idem-c1-v1"})
    verdict = harness.engine.limit_review(change.id).verdicts[2]
    assert verdict.refused and "cc" in verdict.detail


def test_clause_three_refuses_a_recipient_who_is_not_the_sender():
    harness = _harness()
    change, _ = _benign(harness, email_args={
        "to": ["dave@example.com"], "subject": "Re: Our usual cafe has closed", "body": REPLY,
        "in_reply_to": "m1", "thread_id": "t1", "change_id": "c1", "idempotency_key": "idem-c1-v1"})
    verdict = harness.engine.limit_review(change.id).verdicts[2]
    assert verdict.refused and "not the person who wrote" in verdict.detail


def test_clause_four_refuses_a_fragment_from_an_unlisted_source():
    harness = _harness()
    change, _ = _benign(harness, fragments=[{"source": "model_free_text", "text": REPLY}])
    verdict = harness.engine.limit_review(change.id).verdicts[3]
    assert verdict.refused and "allowlist" in verdict.detail


def test_clause_four_refuses_a_body_that_is_not_its_own_fragments():
    """The clause that makes "no free text from the model" mean something: the body has to BE the
    render of the declared pieces, so a sentence the model added is a body that does not match."""
    harness = _harness()
    change, _ = _benign(harness, email_args={
        "to": ["alex@example.com"], "subject": "Re: Our usual cafe has closed",
        "body": REPLY + " Ps. I also told the whole team.",
        "in_reply_to": "m1", "thread_id": "t1", "change_id": "c1", "idempotency_key": "idem-c1-v1"})
    verdict = harness.engine.limit_review(change.id).verdicts[3]
    assert verdict.refused and "no source declared" in verdict.detail


def test_clause_five_refuses_a_plan_that_already_ran():
    harness = _harness()
    change, cs = _benign(harness)
    harness.changesets.mark(cs.id, cs.version, "approved")
    verdict = harness.engine.limit_review(change.id).verdicts[4]
    assert verdict.refused and "already" in verdict.detail


def test_clause_six_refuses_an_email_that_raised_two_changes():
    """One reply per email. Two changes from one message is the state where "at most one" stops
    being a statement about the plan and starts being one about the mailbox."""
    from privatemesh.changes import Change

    harness = _harness()
    change, _ = _benign(harness)
    harness.engine._changes["c2"] = Change(
        id="c2", thread_id="t1", message_id="m1", from_name="Alex Doe",
        from_address="alex@example.com", subject="Our usual cafe has closed",
        excerpt="", received_at=at("08:05"), sender_pinned="alex@example.com")
    verdict = harness.engine.limit_review(change.id).verdicts[4]
    assert verdict.refused and "2 changes" in verdict.detail


def test_every_clause_the_engine_checks_is_a_clause_the_owner_was_shown():
    """A rule the engine enforces but the catalogue does not name would be a rule the owner never
    saw — and the decision row would carry `UNKNOWN_CLAUSE` where the sentence should be."""
    harness = _harness()
    change, _ = _benign(harness)
    review = harness.engine.limit_review(change.id)
    assert [v.id for v in review.verdicts] == [c.id for c in limits.CLAUSES]
    for verdict in review.verdicts:
        assert verdict.text == limits.clause(verdict.id).text
        assert verdict.text != limits.UNKNOWN_CLAUSE
    assert review.in_limit and review.failed is None


# ------------------------------------------------------------------ across a restart ------------
def test_the_pins_the_clauses_read_survive_a_restart(tmp_path):
    """Clauses 1 and 4 are checked at execute time against snapshots taken at PREPARATION — which
    meeting this email is about, and what the reply was composed from. A snapshot that did not
    survive a restart would refuse every change prepared before it, so the limits would silently
    stop covering anything across a reboot: the fail-closed direction, and still a bug."""
    from privatemesh.changes import Change, ChangeStore

    path = str(tmp_path / "changes.db")
    ChangeStore(path=path).save(Change(
        id="c1", thread_id="t1", message_id="m1", from_name="Alex Doe",
        from_address="alex@example.com", subject="s", excerpt="", received_at=at("08:05"),
        event_pinned="ev1", reply_fragments=[{"source": "template", "text": REPLY}],
        searches_used=2, executed_by="limit"))

    back = ChangeStore(path=path).load()[0]
    assert back.event_pinned == "ev1"
    assert back.reply_fragments == [{"source": "template", "text": REPLY}]
    assert back.searches_used == 2, "a spent research budget must not read as unspent"
    assert back.executed_by == "limit", "who ran it is part of the record, not of the process"


def test_a_bundle_the_limit_cleared_but_could_not_run_goes_back_to_the_owner():
    """The stranded-change hazard, and why `_hand_back` exists.

    Between the clause check and the execution the bundle is admitted — an `approved` row, written
    before anything touches a provider. If the executor then fails for a reason that is NOT
    staleness, the change would be left `approved` and `prepare` returns early on `approved`, so
    the owner's own approve would 409 with "already approved" and the review screen would say
    approved over a meeting that never moved. Handing it back is what keeps a failed limit run
    from being the one state the owner cannot act on.
    """
    harness = _harness()
    change, cs = _benign(harness)
    service = _standing(harness, WITHIN_LIMITS)

    def boom(*_args, **_kwargs):
        raise RuntimeError("the executor fell over")

    harness.executor.execute = boom

    body = service.prepare(change.id)

    assert body["stage"] == "proposal_ready"
    assert body["executed_by"] is None
    assert harness.changesets.for_change(change.id).status == "pending", \
        "the bundle was left approved, so the owner cannot act on it"
    assert harness.mail.sent == [] and harness.calendar.get_event("ev1").start == at("15:00")
    rows = harness.engine.decisions_for(change.id)
    assert any(r["subject"].startswith("The standing limits cleared this plan, but it did not run")
               for r in rows), "a cleared plan that stopped has to say so"


def test_a_real_preparation_records_the_snapshots_the_clauses_read():
    """The capture itself, driven through the real `prepare` rather than hand-set.

    Clauses 1, 4 and 6 read `event_pinned`, `reply_fragments` and `searches_used`. If `_prepare`
    stopped recording them the clauses would refuse everything — fail-closed, and invisible, since
    a limit that never fires looks exactly like a limit with nothing to do. So the capture is
    asserted here on a change the ENGINE built, from the case corpus the redteam uses.
    """
    from privatemesh.boundary import ALLOWED_FRAGMENT_SOURCES, Fragment, OutboundEmail
    from privatemesh.redteam import CASES_DIR, CompromisedModel, load_cases, sample_ports, _message_for
    from privatemesh.wiring import build_sample

    case = {c["id"]: c for c in load_cases(CASES_DIR)}["08-benign-control"]
    ports = sample_ports()
    ports.mail.deliver(_message_for(case))
    wiring = build_sample(ports=ports, llm=CompromisedModel(case, [], []))
    try:
        change = wiring.engine.detect(None)[0]
        wiring.engine.prepare(change.id)

        assert change.stage == READY, \
            "the control case has to reach a bundle for this to mean anything"
        assert change.event_pinned, "clause 1 reads the meeting pinned at preparation"
        assert change.reply_fragments, "clause 4 reads the reply's own decomposition"
        assert all(f["source"] in ALLOWED_FRAGMENT_SOURCES for f in change.reply_fragments)
        # The assertion that makes the capture worth having: what was recorded is enough to CHECK
        # the reply the plan would send, not merely enough to describe it. This is clause 4's own
        # test, run against a change the engine built rather than one this file assembled.
        mail = wiring.changesets.for_change(change.id).email.arguments
        composed = OutboundEmail(
            to=tuple(mail["to"]), subject=mail["subject"], body=mail["body"],
            fragments=tuple(Fragment(source=f["source"], text=f["text"])
                            for f in change.reply_fragments),
            in_reply_to=mail["in_reply_to"], template_body=change.reply_template,
            subject_template=change.reply_subject_template,
            subject_fragments=tuple(Fragment(source=f["source"], text=f["text"])
                                    for f in change.reply_subject_fragments))
        assert composed.body == composed.rendered()
        assert change.searches_used >= 1, "clause 6 reads a research budget the run actually spent"
    finally:
        # The sample wiring may be watching. Nothing here starts it, but a test that leaves a
        # watcher behind leaves it for the next test too.
        wiring.stop()
