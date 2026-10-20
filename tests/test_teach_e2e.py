"""Teach Mode: the owner types a rule, and it becomes a skill the product then applies.

The claim this file exists to make false-or-true is the one a reviewer raised against Concept D:
that a build with ONE hard-coded learnable capability cannot honestly say it "learns skills". Here
the owner writes the rule, and everything after that is the Concept D loop unchanged — the same
scripted sample model answering the real prompts, the same isolated runner, the same different-family
review, the same approval by hash, the same library, the same `_apply_skill` stage.

What is pinned, and why each one is worth a test:

* **the flow**: teach → generate → grade → review → approve → a plan the rule narrows;
* **the direction**: a taught skill may take an option away and may never put one back, refused at
  grading AND at run time, with a mutation control proving the run-time refusal has teeth;
* **the cost of a refusal**: a rule that is too long, or that asks for an action, is refused having
  spent nothing — the assertion is that the transport was never touched;
* **the intersection**: two taught rules compose, and an option survives only if both keep it;
* **revocation**: a revoked rule stops shaping plans.

Nothing here reaches into `learn.py` to make the loop succeed. The two things these tests DO drive
from outside are the ones a browser drives too: which times the sender offered (the model's extract
answer, answered by this file's own transport) and which rule the owner typed.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from privatemesh import wiring as wm
from privatemesh.api import default_app
from privatemesh.changes import EXTRACT_SYSTEM
from privatemesh.config import Settings
from privatemesh.learn import INPUT_SCHEMA, OUTPUT_SCHEMA, TEACH_GENERATE_SYSTEM, reference_run
from privatemesh.llm import TokenFactory
from privatemesh.ports import MailMessage
from privatemesh.sim import SAMPLE_DATE, build_sample_ports
from privatemesh.skillrt import SkillManifest
from privatemesh.teach import (MAX_RULE_CHARACTERS, example_problems, invariant_cases,
                               schema_problems, taught_name, widenings)

SETTINGS = Settings(api_key="sample-no-key", base_url="https://tf.test/v1/",
                    model_fast="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                    model_reason="nvidia/nemotron-3-super-120b-a12b",
                    model_deep="nvidia/Nemotron-3-Ultra-550b-a55b", tavily_key="")

DAY = PLAN_DAY = "2026-09-19"
OFFSET = "+01:00"

RULE = "never before 10am"
OTHER_RULE = "no meetings after 12pm"

#: The offered times, chosen against the sample's own calendar so that ONLY the rules decide. On
#: 2026-09-19 the day holds a 09:15 standup, an 11:00–11:45 sync, a 13:00 panel, a 14:00–14:30 call
#: and a protected 16:30–17:15 with a 45-minute buffer (so nothing may run into 15:45). Every slot
#: below except `BUFFERED` is free, so every refusal below is the rule's and not the calendar's.
EARLY = ("09:30", "10:15")        # free, and the first thing "never before 10am" removes
NOON = ("12:00", "12:45")         # free, and kept by both rules — the intersection
AFTERNOON = ("15:00", "15:45")    # free, and kept by "never before 10am"
LATE = ("14:45", "15:30")         # free, and the first thing "no meetings after 12pm" removes
BUFFERED = ("16:00", "16:45")     # NOT free: it ends inside the buffer kept before 16:30


def _at(hhmm: str) -> str:
    return f"{PLAN_DAY}T{hhmm}:00{OFFSET}"


def _options(*pairs) -> list[dict]:
    return [{"start": _at(s), "end": _at(e), "phrase": s[:2]} for s, e in pairs]


def _recorded_model(recorder: list[dict], *, offers=None, teach_answer=None):
    """A transport that answers the REAL prompts, recording every request on the way.

    `offers` replaces only the EXTRACT answer, which is how a test says what the sender proposed —
    the same thing the sender's email says in the sample, read by the same phase. `teach_answer`
    replaces only the taught generator's answer, which is how a test supplies an artifact the sample
    would never write. Everything else, including both learning prompts, comes from
    `wm.sample_reply`, so what these tests exercise is the sample's own scripted model and not a
    double's idea of one.
    """
    def factory(gate):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            recorder.append(body)
            system = next((str(m.get("content") or "") for m in (body.get("messages") or [])
                           if m.get("role") == "system"), "")
            if offers is not None and system.startswith(EXTRACT_SYSTEM):
                content = json.dumps({"request_excerpt": "could we meet at 9:30 or 3?",
                                      "options": _options(*offers)})
            elif teach_answer is not None and system.startswith(TEACH_GENERATE_SYSTEM):
                content = json.dumps(teach_answer)
            else:
                content = wm.sample_reply(body)
            return httpx.Response(200, json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 300, "total_tokens": 1000}})

        http = httpx.Client(base_url=SETTINGS.base_url, timeout=5,
                            transport=httpx.MockTransport(handler))
        return TokenFactory(SETTINGS, http, boundary=gate)

    return factory


@contextlib.contextmanager
def _product(tmp_path, **kw):
    """The sample product, watching, over a real data directory.

    A directory rather than `:memory:`, because the claim is about the SECOND plan and a library
    that vanishes with the process would make "the rule is still there" untestable. The ports come
    back because making a second email arrive is the only way to ask for a second plan.
    """
    recorder: list[dict] = []
    ports = build_sample_ports(SAMPLE_DATE)
    wired = wm.build_sample(settings=SETTINGS, ports=ports,
                            llm=_recorded_model(recorder, **kw), path=tmp_path / "pm")
    wired.start()
    try:
        yield wired, TestClient(default_app(SETTINGS, demo=True, v2=wired.service)), recorder, ports
    finally:
        wired.stop()


@pytest.fixture
def product(tmp_path):
    """The two slots the headline flow is about: one the rule removes, one it keeps.

    `BUFFERED` rides along as a third offer the VALIDATOR refuses, because the widening tests need
    a slot the trusted rules already said no to: a skill can only be caught reaching past them if
    there is something there to reach past. It never affects a plan, since it never fits.
    """
    with _product(tmp_path, offers=[EARLY, AFTERNOON, BUFFERED]) as built:
        yield built


# ---------------------------------------------------------------- helpers ----------------------
def _change_ids(client) -> list[str]:
    rows = client.get("/v2/today", params={"date": DAY}).json()["changes"]
    return [c["id"] for c in sorted(rows, key=lambda c: c["received_at"])]


def _change_id(client, *, tries: int = 400) -> str:
    for _ in range(tries):
        ids = _change_ids(client)
        if ids:
            return ids[0]
        time.sleep(0.01)
    raise AssertionError("the watcher never surfaced a change")


def _later_message(ports, *, tag: str = "2") -> MailMessage:
    """A second email from the same sender, five minutes later, asking for the same move.

    A real second MESSAGE rather than the same change prepared twice: the plan for one change is
    cached the moment it is prepared, and "the rule shaped the next plan" is a claim about a change
    that did not exist when the rule was taught.
    """
    first = ports.mail.get_thread("thread-alex")[0]
    return dataclasses.replace(
        first, id=f"msg-alex-teach-{tag}", rfc822_message_id=f"<alex-teach-{tag}@example.com>",
        received_at="2026-09-19T09:05:00.000Z", subject="Re: Our usual café has closed",
        body_text="Same again — could we meet near King's Cross at 9:30 or 3?\n\nAlex")


def _second_change(client, first_id: str, wired, ports, *, tag: str = "2") -> str:
    ports.mail.deliver(_later_message(ports, tag=tag))
    wired.tick()
    for _ in range(400):
        other = [c for c in _change_ids(client) if c != first_id]
        if other:
            return other[0]
        time.sleep(0.02)
    raise AssertionError("the second email never became a second change")


def _await_change(client, change_id: str, stage: str, *, tries: int = 3000) -> dict:
    body: dict = {}
    for _ in range(tries):
        body = client.get(f"/v2/changes/{change_id}").json()
        if body["stage"] == stage:
            return body
        time.sleep(0.02)
    raise AssertionError(f"stage {body.get('stage')!r} never reached {stage!r}")


def _prepare(client, change_id: str) -> dict:
    client.post(f"/v2/changes/{change_id}/prepare")
    return _await_change(client, change_id, "proposal_ready")


def _approve_change(client, change_id: str, body: dict) -> dict:
    """Approve exactly the bundle `body` describes, and answer with the change as it now stands.

    `version` lives at the top level of the change, not inside `proposal`: it is the version of the
    BUNDLE the owner is looking at, and `proposal` is what that bundle will do."""
    approved = client.post(f"/v2/changes/{change_id}/approve", json={"version": body["version"]})
    assert approved.status_code == 200, approved.text
    return approved.json()


def _calendar_action(body: dict) -> dict:
    return next(a for a in body["proposal"]["actions"] if a["kind"] == "calendar_update")


def _await_learning(client, learning_id: str, *, tries: int = 8000) -> dict:
    body: dict = {}
    for _ in range(tries):
        body = client.get(f"/v2/learning/{learning_id}").json()
        if body["stage"] in ("ready_for_review", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"learning stage {body.get('stage')!r} never settled")


def _teach(client, rule: str) -> dict:
    started = client.post("/v2/teach", json={"rule": rule})
    assert started.status_code == 200, started.text
    learning = _await_learning(client, started.json()["learning_id"])
    assert learning["stage"] == "ready_for_review", learning
    return learning


def _approve(client, learning: dict) -> dict:
    done = client.post(f"/v2/learning/{learning['id']}/approve",
                       json={"identity": learning["manifest"]["identity"]})
    assert done.status_code == 200, done.text
    return done.json()


def _user_text(recorder: list[dict]) -> str:
    """Every user message the provider was sent, joined — the payload side of the prompt."""
    return "\n".join(str(m.get("content") or "")
                     for body in recorder for m in (body.get("messages") or [])
                     if m.get("role") == "user")


def _system_prompts(recorder: list[dict]) -> list[str]:
    """Every system message the provider was sent — which phase each request was for."""
    return [str(m.get("content") or "")
            for body in recorder for m in (body.get("messages") or [])
            if m.get("role") == "system"]


# ---------------------------------------------------------------- the headline flow ------------
def test_a_taught_rule_narrows_a_real_plan(product):
    """The whole of Teach Mode, once, through the API the web app calls.

    "never before 10am" is the owner's sentence, not the build's. It goes to the generator, comes
    back as code, is graded on cases it never saw, is reviewed by a different model family and is
    approved by hash — and the next plan genuinely loses its 09:30 option while keeping 15:00.
    """
    wired, client, recorder, ports = product
    change_id = _change_id(client)
    before = _prepare(client, change_id)
    assert before["proposal"]["skill"] is None, "nothing has been taught yet"
    assert before["proposal"]["start"] == _at("09:30"), (
        "both offered times fit before the rule is taught and the earliest is proposed, so the "
        "change in the next plan is the rule's doing and not the calendar's")

    learning = _teach(client, RULE)
    assert learning["rule"] == RULE
    assert learning["skill_name"] == taught_name(RULE)
    assert learning["change_id"] == "", "a taught rule is not about one change"
    assert learning["purpose_plain"].endswith(RULE), learning["purpose_plain"]

    # The two attempts the sample's scripted model produces: one that reaches outside its
    # declaration and is stopped, then one that computes. Same loop and same evidence as Concept D.
    first, second = learning["attempts"]
    assert [a["version"] for a in learning["attempts"]] == ["1.0.0", "1.0.1"]
    assert first["review"]["verdict"] == "reject"
    assert any(v["kind"] == "import" for v in first["violations"]), first["violations"]
    assert second["review"]["verdict"] == "approve"
    assert second["held_out"]["passed"] > 0 and second["held_out"]["failed"] == 0, second
    assert second["invariants"]["passed"] == second["invariants"]["cases"] > 0
    assert second["invariants"]["runs"] == 2 * second["invariants"]["cases"], (
        "every case is run twice, because one of the invariants is determinism")

    # The labelled examples the owner reads on the approval screen, and approves WITH the skill.
    examples = learning["examples"]
    assert {e["kept"] for e in examples} == {True, False}, (
        "examples that all agree do not show the owner where the line is")
    assert all(set(e) == {"input", "kept", "why"} for e in examples), (
        "the contract publishes three fields; the machine-checkable `case` stays behind")
    assert all(e["input"] and e["why"] for e in examples), examples

    manifest = learning["manifest"]
    assert manifest["rule"] == RULE
    assert manifest["examples"] == examples, "the screen and the approval are about the same cases"
    assert manifest["identity"] == second["identity"]
    assert manifest["never"], "a consent screen that says nothing about limits is not one"
    assert _approve(client, learning)["state"] == "active"

    assert [(s["name"], s["rule"], s["state"]) for s in client.get("/v2/skills").json()] == [
        (taught_name(RULE), RULE, "active")]

    # The plan AGAIN — a fresh change offering the same two times.
    later_id = _second_change(client, change_id, wired, ports)
    proposal = _prepare(client, later_id)["proposal"]
    assert proposal["start"] == _at("15:00"), proposal["start"]
    assert proposal["skill"]["used"] is True, proposal["skill"]
    assert proposal["skill"]["rule"] == RULE
    assert proposal["skill"]["identity"] == manifest["identity"]
    assert proposal["skill"]["reuse_line"].startswith(f"Using your skill: {taught_name(RULE)}")

    # The narrowing itself: 09:30 is gone, and it is the RULE that removed it rather than the
    # validator — `taught_rule` is a code only a taught skill can set, and the sentence names the
    # line the owner drew.
    rejected = {r["name"]: r for r in proposal["rejected"]}
    assert rejected["09:30–10:15"]["by"] == taught_name(RULE)
    assert rejected["09:30–10:15"]["code"] == "taught_rule"
    assert "10:00" in rejected["09:30–10:15"]["reason"], rejected["09:30–10:15"]["reason"]
    assert proposal["skill"]["narrowed"] == ["09:30–10:15"]
    assert proposal["overreach"] == [], "narrowing is the job; nothing was reached past"

    why = client.get(f"/v2/changes/{later_id}/why").json()
    assert why["learned_skill"]["identity"] == manifest["identity"]
    assert any("learned skill" in u["label"].lower() for u in why["used"]), why["used"]

    # The owner's sentence is what the model was asked to implement, verbatim.
    assert RULE in _user_text(recorder)


def test_a_generated_taught_skill_that_widens_is_refused_at_grading(tmp_path):
    """The direction, enforced by the grader rather than promised by the prompt.

    The generator answers with code that says every offered time fits. Nothing else about it is
    wrong: it is well-formed, it imports nothing forbidden, it proposes no effects and it answers
    deterministically. The INVARIANT check is what stops it — the same check that runs before the
    owner is shown an approval screen — and the learning is reported `failed`, not installed.
    """
    answer = _widening_answer()
    with _product(tmp_path, offers=[EARLY, AFTERNOON], teach_answer=answer) as built:
        wired, client, _recorder, _ports = built
        started = client.post("/v2/teach", json={"rule": RULE})
        learning = _await_learning(client, started.json()["learning_id"])
        assert learning["stage"] == "failed", learning
        assert learning["attempts"], "a failure with no attempt on it explains nothing"
        assert all(a["ok"] is False for a in learning["attempts"])
        hits = [v for a in learning["attempts"] for v in a["violations"]
                if v["kind"] == "invariant"]
        assert hits, [a["violations"] for a in learning["attempts"]]
        assert any("fits=True" in v["detail"] for v in hits), hits
        assert client.get("/v2/skills").json() == [], "a refused skill is never installed"
        assert wired.library.taught() == []


def test_a_taught_skill_that_widens_is_refused_at_run_time(product):
    """The same direction, enforced again on the path that actually decides a plan.

    Grading happens once, in a sandbox, at learning time. The bytes that RUN are read back from the
    library later, so the direction is checked again there — against the owner's own rules, over the
    actual inputs, in `changes._run_one`. A skill that says a slot fits where `fit.py` says it does
    not loses, the event lands on the change where the owner can see it, and the validator's plan
    stands unchanged.
    """
    wired, client, _recorder, ports = product
    _install_widening_taught(wired.library, RULE)
    assert wired.library.taught(), "the row must be live for this to be about the guard"

    change_id = _change_id(client)
    body = _prepare(client, change_id)
    proposal = body["proposal"]
    # The fixture's second slot ends inside the protected buffer, so one of the two is a slot the
    # validator refuses — which is what makes the widening skill's answer a WIDENING here rather
    # than merely a disagreement.
    overreach = proposal["overreach"]
    assert len(overreach) == 1, overreach
    assert overreach[0]["skill"] == taught_name(RULE)
    assert overreach[0]["attempted"] == "widen what fits"
    assert overreach[0]["blocked_by"] == "manifest"
    assert "fits=True" in overreach[0]["detail"], overreach[0]
    assert proposal["skill"]["used"] is False, "a widening skill must not decide the plan"
    assert [s["used"] for s in proposal["skill"]["skills"]] == [False]
    # The owner's rules still decide, and the slot the validator refused is still refused.
    assert proposal["start"] == _at("09:30"), proposal["start"]
    assert {r["name"]: r["code"] for r in proposal["rejected"]}["16:00–16:45"] == (
        "overlaps_protected_buffer")
    assert wired.library.active(taught_name(RULE))["uses"] == 0, "a refused run is not a use"

    # ...and the refusal has to hold all the way to the PROVIDER, not only in the displayed
    # proposal. A `proposal` is a document this build renders; the arguments `execute.py` hands the
    # port are the thing the calendar and the sender receive, and a guard that only shaped the
    # first would leave the second open. The owner approves, and the meeting moves to the slot the
    # validator chose — the buffered one the skill said fits is nowhere near it.
    _approve_change(client, change_id, body)
    event = ports.calendar.get_event(_calendar_action(body)["event_id"])
    assert event.start == _at("09:30"), "the provider holds the validator's slot, not the skill's"
    assert event.end == _at("10:15"), event.end
    assert [m["to"] for m in ports.mail.outbox] == [("alex@example.com",)], \
        "and the only message is the one reply to the person who wrote"


def test_the_widening_guard_is_what_stops_it(product):
    """The mutation control for the run-time refusal above.

    `changes.widenings` is replaced by a function that never finds anything, and the SAME artifact
    that was just refused is used again. A guard whose removal changes nothing is not a guard: with
    the check gone the widening skill is accepted and its use is counted, and the only reason the
    plan is still right is that a skill can subtract and cannot add. That is exactly the property
    the test above protects, and this is the check that says so.

    The run goes all the way to the PORT, because "the plan is still the validator's" and "the
    provider was handed the validator's arguments" are different claims and only the second one is
    about the outside world. With the guard gone the skill's answer is accepted and its use is
    counted — and the calendar arguments are still the plan's, field for field.
    """
    wired, client, _recorder, ports = product
    _install_widening_taught(wired.library, RULE)
    import privatemesh.changes as changes_mod

    original = changes_mod.widenings
    changes_mod.widenings = lambda inputs, result: []
    try:
        change_id = _change_id(client)
        body = _prepare(client, change_id)
    finally:
        changes_mod.widenings = original
    proposal = body["proposal"]
    assert proposal["overreach"] == [], "with the guard gone the event is never recorded"
    assert proposal["skill"]["used"] is True, (
        "with the guard gone the widening skill is accepted — which is what the test above is for")
    assert proposal["start"] == _at("09:30")
    assert {r["name"]: r["code"] for r in proposal["rejected"]}["16:00–16:45"] == (
        "overlaps_protected_buffer"), "and yet the plan is still the validator's"

    # Reach `execute.py`: the accepted widening skill still cannot move a field the port receives.
    _approve_change(client, change_id, body)
    event = ports.calendar.get_event(_calendar_action(body)["event_id"])
    assert (event.start, event.end) == (_at("09:30"), _at("10:15")), \
        "the skill's answer does not reach the calendar even with the guard removed"
    assert len(ports.mail.outbox) == 1, "and the reply is the plan's single one"
    assert ports.mail.outbox[0]["headers"]["To"] == "alex@example.com", \
        "the widening skill cannot add a recipient"


def test_an_action_shaped_rule_is_refused_without_asking_a_model(product):
    """A taught skill computes; it never acts. The refusal has to happen BEFORE the spend.

    The assertion is on the transport, not on a counter: no request reached it, so no token was
    bought, no thread was started and no library row was left behind. A refusal that had already
    asked a model would be a refusal that cost money, and where `check_rule` runs is the whole
    difference between the two.
    """
    wired, client, recorder, _ports = product
    # Snapshot first: the routine's own startup tick has already classified and read the sample's
    # email, and those calls are the product working, not this test's doing.
    calls_before, requests_before = wired.llm.usage.calls, len(recorder)
    for rule, code in (
            ("email my boss whenever a meeting is proposed", "rule_asks_for_an_action"),
            ("book the room for any client meeting", "rule_asks_for_an_action"),
            ("remind me to cancel anything on a Friday", "rule_asks_for_an_action"),
            ("x" * (MAX_RULE_CHARACTERS + 1), "rule_too_long"),
            ("   ", "empty_rule")):
        refused = client.post("/v2/teach", json={"rule": rule})
        assert refused.status_code == 422, (rule, refused.status_code, refused.text)
        assert refused.json()["code"] == code, refused.json()
        assert refused.json()["detail"], "a refusal the owner cannot act on is not a refusal"

    assert len(recorder) == requests_before, (
        "a refused rule must not have asked a model anything")
    assert wired.llm.usage.calls == calls_before, "and must not have bought a token"
    assert not [s for s in _system_prompts(recorder) if s.startswith(TEACH_GENERATE_SYSTEM)], (
        "and in particular it must never have reached the generator")
    assert wired.library.summaries() == [], "and must not have left a row behind"

    # ...and a rule that is only LONG is accepted at the limit, so the gate is a bound rather than a
    # blanket refusal of anything unusual. The last request above was the empty one, so this also
    # shows the transport works again the moment a rule is usable.
    at_limit = "no meetings before 10am " + "x" * (MAX_RULE_CHARACTERS - 24)
    assert len(at_limit) == MAX_RULE_CHARACTERS
    assert client.post("/v2/teach", json={"rule": at_limit}).status_code == 200
    assert recorder, "a rule inside the bound does reach the generator"


def test_two_taught_rules_compose_by_intersection(tmp_path):
    """Teaching twice is two rules, not a replacement — and an option survives only if BOTH keep it.

    "never before 10am" removes 09:30. "no meetings after 12pm" removes 14:45. 12:00 is kept by
    both, so 12:00 is the plan: the intersection, not the union and not whichever rule was taught
    last. The union would have kept all three, and a last-writer-wins library would have kept two.
    """
    with _product(tmp_path, offers=[EARLY, NOON, LATE]) as built:
        _wired, client, _recorder, _ports = built
        _approve(client, _teach(client, RULE))
        _approve(client, _teach(client, OTHER_RULE))
        assert sorted(s["rule"] for s in client.get("/v2/skills").json()) == sorted(
            [RULE, OTHER_RULE]), "two rules, two live skills"

        proposal = _prepare(client, _change_id(client))["proposal"]
        assert proposal["start"] == _at("12:00"), proposal["start"]
        assert sorted(proposal["skill"]["narrowed"]) == ["09:30–10:15", "14:45–15:30"]
        assert sorted(s["rule"] for s in proposal["skill"]["skills"]) == sorted([RULE, OTHER_RULE])
        assert proposal["overreach"] == []
        # Only the RULE-narrowed entries carry `by`; the validator's own refusals and the venue
        # rejections do not, which is what makes `by` the thing that names the rule that decided.
        by = {r["name"]: r["by"] for r in proposal["rejected"] if "by" in r}
        assert by == {"09:30–10:15": taught_name(RULE), "14:45–15:30": taught_name(OTHER_RULE)}


def test_a_revoked_taught_rule_stops_applying(product):
    """Revocation is about the rule the owner withdrew, checked on a LATER plan.

    A plan already prepared is a decision the owner may still approve, so revocation does not reach
    into it. What it does is stop the rule being consulted at all — not refused, not overreached,
    simply absent, which is the difference between the two assertions below.
    """
    wired, client, _recorder, ports = product
    first_id = _change_id(client)
    _approve(client, _teach(client, RULE))
    assert _prepare(client, first_id)["proposal"]["skill"]["used"] is True

    revoked = client.post(f"/v2/skills/{taught_name(RULE)}/revoke")
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert wired.library.taught() == []

    after = _prepare(client, _second_change(client, first_id, wired, ports, tag="revoke"))["proposal"]
    assert after["skill"] is None, (
        "a revoked rule must not shape a plan; the proposal should look as it did before")
    assert after["overreach"] == [], "a revoked skill was not refused, it was absent"
    assert after["start"] == _at("09:30"), "without the rule, 09:30 fits and is proposed again"
    assert client.post(f"/v2/skills/{taught_name(RULE)}/revoke").status_code == 404


# ---------------------------------------------------------------- unit-level pins --------------
def test_the_invariant_cases_cover_every_rule_the_reference_applies():
    """The grader's cases are fixed, and between them they exercise every branch of the trusted
    validator. A case set that missed `overlaps_busy` would grade every taught skill on a question
    it never asked, and the invariant would be silent about the thing it exists to check."""
    codes = set()
    for case in invariant_cases():
        codes |= {o["code"] for o in reference_run(case)["options"]}
    assert {"fits", "empty_slot", "in_the_past", "outside_day", "overlaps_protected",
            "overlaps_protected_buffer", "overlaps_busy"} <= codes, sorted(codes)


def test_widenings_sees_every_shape_of_widening_and_nothing_else():
    """The one direction, at the unit level: an extra option, a kept slot the validator refused, and
    — the control — a NARROWED answer, which must not be reported. Without the control this test
    would pass for a guard that flags everything."""
    case = dict(invariant_cases()[1])
    expected = reference_run(case)
    honest = {"options": [dict(o) for o in expected["options"]], "ranked": []}
    assert widenings(case, honest) == [], "answering as the validator does is not widening"

    narrowed = {"options": [dict(o, fits=False, code="taught_rule") for o in expected["options"]],
                "ranked": []}
    assert widenings(case, narrowed) == [], "removing options is the job, not a fault"

    widened = {"options": [dict(o, fits=True) for o in expected["options"]], "ranked": []}
    assert widenings(case, widened), "keeping a slot the rules refused is widening"

    added = {"options": expected["options"] + [expected["options"][0]], "ranked": []}
    assert widenings(case, added), "answering for more slots than were offered is widening"


def test_the_widening_answer_is_well_formed_and_therefore_a_real_test():
    """The control for the grading test above: that artifact is refused for the DIRECTION and for
    nothing else. Were it malformed, `_envelope_taught` would reject it before a single invariant
    ran, and the test would be evidence about a syntax error rather than about the guard."""
    answer = _widening_answer()
    case = invariant_cases()[1]     # offers a slot the buffer refuses, so widening is possible
    widened = {"options": [dict(o, fits=True) for o in reference_run(case)["options"]], "ranked": []}
    assert schema_problems(widened) == [], schema_problems(widened)
    assert example_problems(answer["examples"]) == [], example_problems(answer["examples"])
    assert widenings(case, widened), "and the guard is what sees it"


def test_the_sample_can_only_teach_rules_it_understands():
    """The fixture's stated limit, pinned so it cannot rot into a silent lie.

    The scripted model answers `{}` for a rule it cannot read, and `learn` turns that into a NAMED
    failure. The alternative — answering with the one rule it knows for every sentence it is given —
    would demo Teach Mode cheerfully implementing something other than what the owner typed, which
    is the exact failure this feature exists to prevent.
    """
    from privatemesh.sample_skill_code import parse_taught_rule, taught_generation_envelope

    assert parse_taught_rule(RULE) == {"floor": "10:00", "ceiling": "", "days": (),
                                       "understood": True}
    assert parse_taught_rule(OTHER_RULE)["ceiling"] == "12:00"
    assert parse_taught_rule("no meetings on Friday afternoons")["days"] == (4,)
    assert parse_taught_rule("be nice to people")["understood"] is False
    assert taught_generation_envelope({"owner_rule": "be nice to people"}) == {}, (
        "an unreadable rule must produce no envelope at all")

    envelope = taught_generation_envelope({"owner_rule": RULE, "inputs_schema": INPUT_SCHEMA,
                                           "outputs_schema": OUTPUT_SCHEMA})
    assert envelope and len(envelope["examples"]) >= 2
    assert {e["kept"] for e in envelope["examples"]} == {True, False}, (
        "a rule with a line must show the owner both sides of it")
    assert example_problems(envelope["examples"]) == []


# ---------------------------------------------------------------- the artifact under test ------
WIDENING_TAUGHT_MODULE = '''
def run(inputs):
    slots = list((inputs or {}).get("offered_slots") or ())
    return {"options": [{"start": s["start"], "end": s["end"], "fits": True, "code": "fits",
                         "reason": "%s-%s is free" % (s["start"][11:16], s["end"][11:16])}
                        for s in slots],
            "ranked": [{"start": s["start"], "end": s["end"], "venue": None} for s in slots]}
'''

WIDENING_TAUGHT_TESTS = '''import taught_rule


def test_it_answers():
    result = taught_rule.run({"offered_slots": [
        {"start": "2026-09-21T15:00:00+01:00", "end": "2026-09-21T15:45:00+01:00"}]})
    assert result["options"][0]["fits"] is True
'''


def _widening_answer() -> dict:
    """A generator answer whose code says every offered time fits.

    The manifest is exactly the contract's, and the examples are the shape the contract asks for, so
    the ONLY thing wrong with this artifact is the direction it computes in. That is the point: it
    is the invariant check, not the shape check, that has to catch it — a malformed answer would be
    rejected by `_envelope_taught` before a single invariant ran.

    The refused example is deliberately one the code does NOT refuse: that disagreement is the
    artifact's own, and it is caught by the examples check as well as by the invariants. Both are
    true findings about a skill that says everything fits.
    """
    first, second = invariant_cases()[0]["offered_slots"]
    return {"module_code": WIDENING_TAUGHT_MODULE, "tests_code": WIDENING_TAUGHT_TESTS,
            "manifest": {"inputs": INPUT_SCHEMA, "outputs": OUTPUT_SCHEMA,
                         "reads": sorted(INPUT_SCHEMA["properties"]),
                         "proposes": [], "never": ["use the network"]},
            "examples": [
                {"input": "any time at all", "kept": True, "why": "the code says every time fits",
                 "case": {"offered_slots": [dict(first)]}},
                {"input": "a time your own rules already refuse", "kept": False,
                 "why": "it should refuse this one, and the code answers that it fits",
                 "case": {"offered_slots": [dict(second)]}}]}


def _install_widening_taught(library, rule: str) -> dict:
    """A taught row of the shape the owner could have been shown, whose code says everything fits.

    Installed directly because this is about the RUN-time guard, not about how a bad artifact could
    reach the library — the grading test above is where that is settled. Its `rule` is what makes
    `_run_one` hold it to the narrowing direction instead of to exact agreement with the validator.
    """
    manifest = SkillManifest(name=taught_name(rule), version="9.9.9",
                             purpose=f"Applies your rule: {rule}", module="taught_rule",
                             entry="run", inputs=INPUT_SCHEMA, outputs=OUTPUT_SCHEMA,
                             reads=tuple(sorted(INPUT_SCHEMA["properties"])), proposes=(),
                             rule=rule)
    return library.install(manifest=manifest, code=WIDENING_TAUGHT_MODULE,
                           tests=WIDENING_TAUGHT_TESTS,
                           purpose_plain=f"Applies your rule: {rule}")


# ------------------------------------------- a refusal a skill left behind is still a refusal ---
#: The learned rule the regression below needs. `_CEILING_RE` reads it as "nothing may start after
#: 12:00", so it takes `AFTERNOON` (15:00) away — the ONLY slot the validator had passed, since
#: `BUFFERED` (16:00) already crosses the buffer kept before the protected 16:30.
CEILING_RULE = OTHER_RULE


@pytest.mark.parametrize("offers", [(AFTERNOON, BUFFERED), (BUFFERED, AFTERNOON)],
                         ids=["skill-first", "skill-last"])
def test_a_boundary_refusal_is_still_a_refusal_when_no_plan_survives(tmp_path, offers):
    """The fifth review's High: the boundary verdict has to be decided AFTER every narrowing stage.

    The scenario, exactly: the sender offers 15:00 and 16:00; 15:00 is free and 16:00 crosses the
    protected buffer, so the owner's own rules refuse it; then the taught rule removes 15:00 and NO
    plan survives. The version this fixes decided the 16:00 verdict from `any_fit` computed BEFORE
    the skill ran — 15:00 fitted at that moment, so 16:00 was recorded `discarded`, `discarded`
    does not count as a blocked attempt, and the feed hid a real boundary refusal behind a plan that
    no longer existed.

    Both orders are driven because the old defect was invisible in one of them: whether the slot
    the skill removes comes before or after the slot the rules refuse must not change what the owner
    is told. The assertion is made where the owner reads it — the decision rows the API serves and
    the count the feed turns into "Stopped N requests on this thread".
    """
    with _product(tmp_path, offers=list(offers)) as built:
        wired, client, _recorder, _ports = built
        assert _approve(client, _teach(client, CEILING_RULE))["state"] == "active"

        first = _change_id(client)
        second = _second_change(client, first, wired, _ports, tag="-".join(offers[0]))
        client.post(f"/v2/changes/{second}/prepare")
        change = _await_change(client, second, "no_safe_fit")

        assert change["proposal"] is None, "a plan survived, so this is not the scenario"
        rows = client.get(f"/v2/changes/{second}/decisions").json()
        buffered = [r for r in rows if r["subject"].endswith("16:00–16:45")]
        assert buffered, [r["subject"] for r in rows]
        assert [r["verdict"] for r in buffered] == ["refused"], (
            "the slot the owner's own rules refused was recorded as "
            f"{buffered[0]['verdict']!r} because a sibling still fitted at the moment the verdict "
            "was decided — the skill removed that sibling afterwards, so nothing was chosen over "
            "this slot and the refusal the owner must see was hidden")
        assert change["blocked_attempts"] >= 1, (
            "the feed renders this count as 'Stopped N requests on this thread'; a refused boundary "
            "check that does not count is a refusal the owner is never told about")

        # The rule that removed the sibling is on the change, so the two events are told apart:
        # `refused` is the owner's rules refusing a time, and this is the learned rule removing
        # the one that was left. Neither is reported as the other.
        assert change["skill"]["narrowed"] == ["15:00–15:45"], change["skill"]
