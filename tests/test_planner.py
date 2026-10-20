"""The planner: tolerant parsing, strict validation, halt-on-failure, pause-for-approval."""
from __future__ import annotations

import json

from privatemesh.planner import (MAX_PLAN_STEPS, PlanRunner, ToolOutcome, parse_plan,
                                 render_plan, summarise_report, validate)
from privatemesh.verify import FailureKind, classify_failure, recovery_for, verify_step

PLAN = {"goal": "book a table", "steps": [
    {"description": "check the diary", "tool": "notes_read", "arguments": {"query": "dinner"},
     "expected_evidence": "a note about dinner"},
    {"description": "message the restaurant", "tool": "send_message",
     "arguments": {"to": "restaurant", "body": "table for two"}, "expected_evidence": "sent"},
]}


# ------------------------------------------------------------------ parsing -------------------
def test_parse_accepts_a_fenced_json_string():
    plan = parse_plan("```json\n" + json.dumps(PLAN) + "\n```")
    assert plan is not None and plan.goal == "book a table" and len(plan.steps) == 2
    assert plan.steps[1].tool == "send_message" and plan.steps[0].index == 0


def test_parse_accepts_a_plain_dict():
    assert parse_plan(PLAN).steps[0].description == "check the diary"


def test_parse_returns_none_rather_than_raising():
    assert parse_plan("not json at all") is None
    assert parse_plan("[1,2,3]") is None
    assert parse_plan(None) is None
    assert parse_plan({"steps": "nope"}) is None
    assert parse_plan({}) is None                       # no goal and no steps


def test_parse_clips_untrusted_text():
    plan = parse_plan({"goal": "g" * 900, "steps": [
        {"description": "d" * 900, "tool": "notes_read", "expected_evidence": "e" * 900}]})
    assert len(plan.goal) <= 501 and len(plan.steps[0].description) <= 301


def test_parse_coerces_bad_step_fields():
    plan = parse_plan({"goal": "g", "steps": [{"description": "d", "tool": "x", "arguments": "nope"}]})
    assert plan.steps[0].arguments == {}


def test_validate_rejects_an_unknown_tool():
    v = validate(parse_plan(PLAN), {"notes_read"})
    assert v.ok is False and "unknown tool 'send_message'" in " ".join(v.errors)


def test_validate_rejects_empty_and_oversized_plans():
    assert validate(None, set()).ok is False
    assert validate(parse_plan({"goal": "g", "steps": []}), set()).ok is False
    big = {"goal": "g", "steps": [{"description": "d"} for _ in range(MAX_PLAN_STEPS + 5)]}
    assert validate(parse_plan(big), set()).ok is False
    assert validate(parse_plan(PLAN), {"notes_read", "send_message"}).ok is True


def test_render_is_deterministic_and_names_the_tool():
    text = render_plan(parse_plan(PLAN))
    assert "book a table" in text and "[send_message]" in text and "expect: sent" in text


# ------------------------------------------------------------------ failure typing -----------
def test_failures_are_classified_into_typed_kinds():
    assert classify_failure("connection reset by peer") is FailureKind.TRANSIENT
    assert classify_failure("404 not found") is FailureKind.NOT_FOUND
    assert classify_failure("invalid argument: to") is FailureKind.INVALID_INPUT
    assert classify_failure("403 forbidden") is FailureKind.PERMISSION
    assert classify_failure("something odd") is FailureKind.UNKNOWN
    assert recovery_for(FailureKind.TRANSIENT).value == "retry"
    assert recovery_for(FailureKind.PERMISSION).value == "ask_owner"


def test_a_step_that_produced_no_evidence_is_not_verified():
    assert verify_step(ok=True, expected_evidence="a note", summary="(no output)").verified is False
    assert verify_step(ok=True, expected_evidence="a note", summary="found it").verified is True
    assert verify_step(ok=True, expected_evidence="", summary="").verified is True
    assert verify_step(ok=False, expected_evidence="a note", summary="x").verified is False


# ------------------------------------------------------------------ runner --------------------
def _runner(run_tool, approve=lambda rendered: True, run_approved=None):
    return PlanRunner(run_tool=run_tool, approve_plan=approve, run_approved=run_approved)


def _plan():
    return parse_plan(PLAN)


def test_a_clean_plan_runs_every_step_through_the_injected_executor():
    seen = []

    def run_tool(skill, args, task_id):
        seen.append((skill, args, task_id))
        return ToolOutcome(ok=True, summary=f"{skill} did it")

    report = _runner(run_tool).run(_plan(), task_id="t1")
    assert report.completed is True and report.paused is False
    assert [s[0] for s in seen] == ["notes_read", "send_message"]
    assert all(o.verified for o in report.outcomes)
    assert "Plan complete" in report.text


def test_a_failed_step_halts_the_plan_and_is_reported():
    def run_tool(skill, args, task_id):
        if skill == "send_message":
            return ToolOutcome(ok=False, summary="503 unavailable")
        return ToolOutcome(ok=True, summary="read the diary")

    report = _runner(run_tool).run(_plan(), task_id="t2")
    assert report.completed is False and report.outcomes[-1].failure == "transient/retry"
    assert "halted" in report.text and "1/2 steps done" in report.text


def test_a_step_without_evidence_halts_the_plan():
    def run_tool(skill, args, task_id):
        return ToolOutcome(ok=True, summary="(no output)")

    report = _runner(run_tool).run(_plan(), task_id="t3")
    assert report.completed is False and "unverified" in report.outcomes[0].failure


def test_a_denied_stage_runs_nothing():
    calls = []
    report = _runner(lambda *a: calls.append(a) or ToolOutcome(ok=True, summary="x"),
                     approve=lambda rendered: False).run(_plan(), task_id="t4")
    assert report.completed is False and report.reason == "plan_stage_denied"
    assert calls == []


def test_a_transient_failure_is_retried_once_for_a_retriable_skill():
    attempts = {"n": 0}

    def run_tool(skill, args, task_id):
        attempts["n"] += 1
        if skill == "notes_read" and attempts["n"] == 1:
            return ToolOutcome(ok=False, summary="timeout")
        return ToolOutcome(ok=True, summary="read the diary")

    runner = PlanRunner(run_tool=run_tool, approve_plan=lambda r: True,
                        is_retriable=lambda name: name == "notes_read")
    report = runner.run(_plan(), task_id="t5")
    assert report.completed is True
    assert report.outcomes[0].ok is True and attempts["n"] == 3   # 1 retry for step 1, then step 2


def test_a_transient_failure_is_not_retried_for_a_non_idempotent_skill():
    attempts = {"n": 0}

    def run_tool(skill, args, task_id):
        attempts["n"] += 1
        return ToolOutcome(ok=False, summary="timeout")

    runner = PlanRunner(run_tool=run_tool, approve_plan=lambda r: True, is_retriable=lambda n: False)
    runner.run(_plan(), task_id="t6")
    assert attempts["n"] == 1                   # a side effect is never blindly re-fired


def test_a_step_needing_approval_pauses_the_run_instead_of_failing_it():
    def run_tool(skill, args, task_id):
        if skill == "send_message":
            return ToolOutcome(ok=False, summary="needs approval", approval_id="ap-1")
        return ToolOutcome(ok=True, summary="read the diary")

    report = _runner(run_tool).run(_plan(), task_id="t7")
    assert report.paused is True and report.completed is False
    assert report.paused_step == 1 and report.outcomes[-1].approval_id == "ap-1"
    assert "paused" in report.text


def test_a_paused_run_resumes_at_the_same_step_with_the_approval():
    spent = []

    def run_tool(skill, args, task_id):
        if skill == "send_message":
            return ToolOutcome(ok=False, summary="needs approval", approval_id="ap-2")
        return ToolOutcome(ok=True, summary="read the diary")

    def run_approved(skill, args, task_id, approval_id):
        spent.append((skill, approval_id))
        return ToolOutcome(ok=True, summary="message sent")

    runner = _runner(run_tool, run_approved=run_approved)
    first = runner.run(_plan(), task_id="t8")
    second = runner.run(_plan(), task_id="t8", outcomes=first.outcomes,
                        pending_approval="ap-2")
    assert second.completed is True
    assert spent == [("send_message", "ap-2")]
    assert [o.index for o in second.outcomes] == [0, 1]     # resumed, not restarted


def test_the_runner_survives_an_executor_that_raises():
    def run_tool(skill, args, task_id):
        raise RuntimeError("kaboom")

    report = _runner(run_tool).run(_plan(), task_id="t9")
    assert report.completed is False and "error:" in report.outcomes[0].summary


def test_summarise_report_marks_the_paused_step():
    plan = _plan()
    from privatemesh.planner import StepOutcome
    text = summarise_report(plan, (StepOutcome(index=0, ok=False, summary="waiting",
                                               approval_id="ap"),), completed=False, paused=True)
    assert "⏸" in text and "waiting on approval for step 1" in text


# ---- reasoning the model inlined into its answer -----------------------------------------------
def test_parse_plan_strips_an_inline_think_block_before_the_json():
    """Nemotron normally sends reasoning in its own field, but a thinking model can inline it —
    and a `<think>` block in front of the object is the difference between a plan and a retry."""
    raw = "<think>The owner wants a table. I should check the diary first.</think>\n" + json.dumps(PLAN)
    plan = parse_plan(raw)
    assert plan is not None and plan.goal == "book a table" and len(plan.steps) == 2


def test_parse_plan_strips_a_fence_that_wraps_a_think_block():
    raw = "```json\n<think>hmm</think>\n" + json.dumps(PLAN) + "\n```"
    plan = parse_plan(raw)
    assert plan is not None and plan.goal == "book a table"


def test_an_unterminated_think_block_swallows_the_rest():
    """A model cut off mid-thought has produced scratch, not a plan. Parsing the tail as a plan
    would be executing whatever the reasoning happened to contain."""
    assert parse_plan('<think>I was about to write {"goal": "x", "steps": []}') is None


def test_strip_reasoning_leaves_ordinary_text_alone():
    from privatemesh.planner import strip_reasoning
    assert strip_reasoning("just an answer") == "just an answer"
    assert strip_reasoning("") == ""
    assert strip_reasoning("a < b and c > d") == "a < b and c > d"
