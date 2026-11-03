# Ported from the author's earlier private assistant; adapted for PrivateMesh.
"""The planning and execution loop: goal -> ordered steps -> validated -> approved -> executed.

Ported from the prior assistant's `kernel/planner.py`. The design carried over intact:

- the heavy logic lives here, not in the agent, and **every effect is injected** (`run_tool`,
  `approve_plan`, `emit_status`, `publish`), so the whole loop is exercisable with fakes — no live
  model, no HTTP;
- `parse_plan` is tolerant of model framing (fences, prose) and total — a bad plan is `None`, not
  a crash — and `validate` is what actually decides whether a plan may run;
- an un-parseable or invalid plan is NOT executed: the loop reports why and defers, never flails;
- evidence-based uncertainty: a step that "succeeded" but produced nothing is a failure, so the
  plan halts rather than building on an unconfirmed step (see `verify.py`).

Two deliberate adaptations for this repo:

1. **Synchronous.** The prior assistant's runner was `async` because its channel (Telegram inline
   buttons) was. PrivateMesh v0 has no async channel — its approval surface is an HTTP endpoint
   that resumes a *stored* run — so the runner is sync and the whole loop is testable without an
   event loop.
2. **It can pause for an approval.** A step whose skill the broker refuses to run without the
   owner (`needs_approval`) is not a failure: the run halts with `paused=True` and the approval id,
   and `run(..., pending_approval=...)` resumes it at that same step. Step-level approval is still
   re-evaluated by the broker on every step — approving a plan never launders a sensitive action.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .verify import FailureKind, classify_failure, recovery_for, verify_step

log = logging.getLogger(__name__)

# --- Tunable caps (env-overridable where an operator might reasonably change them) ---
MAX_PLAN_STEPS = int(os.environ.get("PM_MAX_PLAN_STEPS", "12"))
MAX_GOAL_LEN = 500
MAX_DESC_LEN = 300
MAX_EVIDENCE_LEN = 300

# Reason tags surfaced to the owner / audit trail.
INVALID_PLAN_REASON = "plan_invalid"
NO_PLAN_REASON = "plan_unparseable"
STAGE_DENIED_REASON = "plan_stage_denied"
NEEDS_APPROVAL_REASON = "needs_approval"


# --------------------------------------------------------------------------- #
# Frozen data (pure, no I/O)                                                   #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlanStep:
    """One ordered step. A step with `tool=None` is a reasoning/report step (no side effect); a
    step with a tool runs through the broker."""

    index: int
    description: str
    tool: Optional[str] = None
    arguments: dict = field(default_factory=dict)
    expected_evidence: str = ""

    def is_action(self) -> bool:
        return bool(self.tool)


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: tuple[PlanStep, ...] = ()

    def is_empty(self) -> bool:
        return not self.steps


@dataclass(frozen=True)
class PlanValidation:
    ok: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class StepOutcome:
    index: int
    ok: bool
    summary: str
    verified: bool = True    # did the step's evidence check out?
    failure: str = ""        # typed "kind/recovery", or "unverified (…)", or "needs_approval"
    approval_id: str = ""    # set when the step is paused awaiting the owner
    content: str = ""        # the skill's output, for the turn's model_saw / receipts view


@dataclass(frozen=True)
class PlanReport:
    """The result of running a plan. `completed` is True only when every step succeeded; otherwise
    the loop halted at the first failure — or paused at the first step needing approval."""

    completed: bool
    outcomes: tuple[StepOutcome, ...] = ()
    text: str = ""
    reason: str = ""
    paused: bool = False
    paused_step: int | None = None


# --------------------------------------------------------------------------- #
# Pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def _clip(value: object, limit: int) -> str:
    """Coerce to a single clipped line — untrusted model output never runs long or injects fence
    markers into a later prompt."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think\b[^>]*>", re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop any inline `<think>…</think>` block from model output, verbatim.

    Nemotron normally sends its reasoning in a separate `reasoning_content` field, but a model
    asked to think can also inline it — and an inline block sitting in front of a JSON object is
    the whole difference between a plan and a wasted repair retry. An UNTERMINATED opener is
    treated as running to the end: everything after it is scratch, not answer."""
    if not text:
        return text
    out = _THINK_BLOCK.sub("", text)
    opener = _THINK_OPEN.search(out)
    return out[:opener.start()] if opener else out


def parse_plan(raw: object) -> Optional[Plan]:
    """Tolerantly turn a model-produced plan (a dict, or a JSON string that may be fenced) into a
    frozen Plan. Returns None on anything unusable — never raises.

    Accepted shape:
        {"goal": str, "steps": [
            {"description": str, "tool": str|null, "arguments": {...},
             "expected_evidence": str}, ...]}
    """
    try:
        data = raw
        if isinstance(data, str):
            # Reasoning first: a fence inside a think-block is not a fence around the plan.
            text = strip_reasoning(data).strip()
            # Strip a ```json … ``` fence if the model wrapped it.
            if text.startswith("```"):
                text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
                if text.lstrip().lower().startswith("json"):
                    text = text.lstrip()[4:]
            data = json.loads(text)
        if not isinstance(data, dict):
            return None

        goal = _clip(data.get("goal", ""), MAX_GOAL_LEN)
        raw_steps = data.get("steps", [])
        if not isinstance(raw_steps, list):
            return None

        steps: list[PlanStep] = []
        # Bound parsing cost by the cap +1 (the +1 still lets validate() report "too many steps" on
        # an over-cap plan) — untrusted model output never drives unbounded work.
        for i, item in enumerate(raw_steps[: MAX_PLAN_STEPS + 1]):
            if not isinstance(item, dict):
                continue
            tool = item.get("tool")
            tool = _clip(tool, 100) if tool else None
            args = item.get("arguments", {})
            if not isinstance(args, dict):
                args = {}
            steps.append(PlanStep(
                index=i,
                description=_clip(item.get("description", ""), MAX_DESC_LEN),
                tool=tool or None,
                arguments=args,
                expected_evidence=_clip(item.get("expected_evidence", ""), MAX_EVIDENCE_LEN),
            ))
        if not goal and not steps:
            return None
        return Plan(goal=goal, steps=tuple(steps))
    except Exception as exc:  # never raise — a bad plan is a None, not a crash
        log.warning("planner.parse_failed: %r", exc)
        return None


def validate(plan: Optional[Plan], known_tools: set[str]) -> PlanValidation:
    """Pure structural validation. A plan is executable only if it has a goal, at least one step,
    is within the step cap, and every action step names a KNOWN tool. `known_tools` is the
    registry snapshot ({t.name for t in …})."""
    errors: list[str] = []
    if plan is None:
        return PlanValidation(ok=False, errors=("no plan could be parsed",))
    if not plan.goal:
        errors.append("plan has no goal")
    if plan.is_empty():
        errors.append("plan has no steps")
    if len(plan.steps) > MAX_PLAN_STEPS:
        errors.append(f"plan has {len(plan.steps)} steps (max {MAX_PLAN_STEPS})")
    for step in plan.steps:
        if step.is_action() and step.tool not in known_tools:
            errors.append(f"step {step.index + 1} names unknown tool '{step.tool}'")
    return PlanValidation(ok=not errors, errors=tuple(errors))


def render_plan(plan: Plan) -> str:
    """Human-readable plan for the approval card + the reply. Deterministic."""
    lines = [f"Plan: {plan.goal}" if plan.goal else "Plan:"]
    for step in plan.steps:
        n = step.index + 1
        head = f"  {n}. {step.description} → [{step.tool}]" if step.is_action() else f"  {n}. {step.description}"
        lines.append(head)
        if step.expected_evidence:
            lines.append(f"       expect: {step.expected_evidence}")
    return "\n".join(lines)


# The JSON schema the planner asks the model to fill. `parse_plan` is still the tolerant safety
# net — a model that ignores the schema falls back to prose, which parses to None, which the agent
# repairs once and then declines to execute.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "tool": {"type": ["string", "null"]},
                    "arguments": {"type": "object"},
                    "expected_evidence": {"type": "string"},
                },
                "required": ["description"],
            },
        },
    },
    "required": ["goal", "steps"],
}

PLAN_SYSTEM_HEADER = (
    "You are PrivateMesh's planner. Break the user's request into a short, ordered plan of "
    "concrete steps. Respond with ONLY a single JSON object — no prose, no markdown fence — of the "
    'form:\n{"goal": "<one line>", "steps": [{"description": "<what this step does>", '
    '"tool": "<a skill name from the list below, or null if the step needs no tool>", '
    '"arguments": {<the skill\'s arguments>}, "expected_evidence": "<what would prove it worked>"}]}'
)


def build_plan_prompt(user_text: str, tools) -> str:
    """Pure builder for the plan-generation system prompt. `tools` is the registry's tool list
    (objects with .name/.description). Deterministic; never raises."""
    lines = [PLAN_SYSTEM_HEADER, f"Use at most {MAX_PLAN_STEPS} steps. Available skills (or null):"]
    listed = False
    for tool in tools or ():
        name = getattr(tool, "name", "")
        if not name:
            continue
        lines.append(f"- {name}: {_clip(getattr(tool, 'description', ''), 120)}")
        # The argument names are part of the contract: without them the model guesses (live
        # Nemotron Super planned `send_message` with no `to`/`body` on 2026-09-18).
        params = getattr(tool, "parameters", None) or {}
        props = params.get("properties") or {}
        if props:
            required = set(params.get("required") or ())
            args = ", ".join(
                f"{k}{'' if k in required else '?'}: {(v or {}).get('type', 'any')}"
                + (f" — {_clip((v or {}).get('description', ''), 60)}" if (v or {}).get("description") else "")
                for k, v in props.items())
            lines.append(f"    arguments: {{{args}}}  (? = optional)")
        listed = True
    if not listed:
        lines.append("- (no skills available — every step must use tool null)")
    lines.append(f"\nUser request: {_clip(user_text, MAX_GOAL_LEN)}")
    return "\n".join(lines)


def summarise_report(plan: Plan, outcomes: tuple[StepOutcome, ...], *, completed: bool,
                     paused: bool = False) -> str:
    """The final 'what was done' report. Honest: it says which step failed, or that it is waiting."""
    if paused:
        header = "⏸ Plan paused — waiting for your approval."
    else:
        header = "✅ Plan complete." if completed else "⚠️ Plan halted."
    lines = [header, f"Goal: {plan.goal}" if plan.goal else "Goal: (unstated)"]
    for outcome in outcomes:
        mark = "✓" if outcome.ok else ("⏸" if outcome.approval_id else "✗")
        line = f"  {mark} step {outcome.index + 1}: {outcome.summary}"
        if not outcome.ok and outcome.failure:
            line += f"  [{outcome.failure}]"
        lines.append(line)
    if paused:
        step = next((o for o in outcomes if o.approval_id), None)
        if step is not None:
            lines.append(f"(waiting on approval for step {step.index + 1})")
    elif not completed:
        done = sum(1 for o in outcomes if o.ok)
        lines.append(f"({done}/{len(plan.steps)} steps done before halting)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Runner (all side effects injected -> testable with fakes)                    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ToolOutcome:
    """What the injected step executor reports back. `approval_id` is set when the broker refused
    to run the skill without the owner — that pauses the run instead of failing it."""

    ok: bool
    summary: str
    approval_id: str = ""
    content: str = ""


# Injected step executor: given (skill_name, arguments, task_id), run the skill through the broker.
RunTool = Callable[[str, dict, str], ToolOutcome]
# Injected approval for a step the owner already approved (given the approval id).
RunApproved = Callable[[str, str, dict, str], ToolOutcome]
# Injected stage approval: given the rendered plan, return True to execute it.
ApprovePlan = Callable[[str], bool]
# Optional progress + lifecycle sinks. Never required.
EmitStatus = Callable[[str], None]
PublishLifecycle = Callable[[str, dict], None]


class PlanRunner:
    """Executes a validated plan step-by-step. Halts on the first failed step, or pauses at the
    first step needing the owner's approval. Never raises.

    All effects are injected so the agent wires real callables and tests inject fakes — no live
    model, no HTTP, no broker needed to exercise the loop."""

    def __init__(self, *, run_tool: RunTool, approve_plan: ApprovePlan,
                 run_approved: Optional[RunApproved] = None,
                 emit_status: Optional[EmitStatus] = None,
                 publish: Optional[PublishLifecycle] = None,
                 is_retriable: Optional[Callable[[str], bool]] = None) -> None:
        self._run_tool = run_tool
        self._approve_plan = approve_plan
        self._run_approved = run_approved
        self._emit = emit_status
        self._publish = publish
        # Given a skill name, is a retry side-effect-safe (idempotent/read-only)? None ⇒ never
        # retry — a retry must never blindly re-fire a side effect.
        self._is_retriable_fn = is_retriable

    def _is_retriable(self, tool_name: str) -> bool:
        if self._is_retriable_fn is None:
            return False
        try:
            return bool(self._is_retriable_fn(tool_name))
        except Exception:
            return False

    def _status(self, message: str) -> None:
        if self._emit is None:
            return
        try:
            self._emit(message)
        except Exception:  # a status sink must never break a plan
            pass

    def _lifecycle(self, event_type: str, data: dict) -> None:
        if self._publish is None:
            return
        try:
            self._publish(event_type, data)
        except Exception:
            pass

    def run(self, plan: Plan, *, task_id: str, outcomes: tuple[StepOutcome, ...] = (),
            pending_approval: str | None = None) -> PlanReport:
        """Approve (stage-level) then execute. Assumes `plan` already validated.

        `outcomes` + `pending_approval` resume a paused run: the recorded outcomes are kept, and
        the step that paused is executed with the owner's approval instead of a fresh policy call.
        A resume never re-approves the stage — the plan was already approved to get this far."""
        try:
            resumed = bool(outcomes)
            if not resumed:
                render = render_plan(plan)
                self._lifecycle("plan.proposed", {"goal": plan.goal, "steps": len(plan.steps)})
                if not self._approve_plan(render):
                    self._lifecycle("plan.denied", {"goal": plan.goal})
                    return PlanReport(completed=False, text="Plan not approved — nothing was run.",
                                      reason=STAGE_DENIED_REASON)
                self._lifecycle("plan.approved", {"goal": plan.goal, "steps": len(plan.steps)})

            done = list(outcomes)
            # A resumed run re-executes the step that paused: its recorded outcome is dropped and
            # the loop restarts AT it, so the approval is spent on the same action, not the next.
            if pending_approval and done and done[-1].approval_id:
                start = done[-1].index
                done = done[:-1]
            else:
                start = len(done)
            total = len(plan.steps)
            for step in plan.steps[start:]:
                self._status(f"Running step {step.index + 1}/{total}: {step.description}")
                if not step.is_action():
                    # A reasoning/report step has no side effect — record it done.
                    done.append(StepOutcome(index=step.index, ok=True,
                                            summary=step.description or "(noted)"))
                    continue
                if step.index == start and pending_approval and self._run_approved is not None:
                    outcome = self._run_and_verify_approved(step, task_id, pending_approval)
                else:
                    outcome = self._run_and_verify_step(step, task_id)
                done.append(outcome)
                if outcome.approval_id:
                    self._lifecycle("plan.paused",
                                    {"goal": plan.goal, "step": step.index + 1,
                                     "approval_id": outcome.approval_id})
                    return PlanReport(completed=False, outcomes=tuple(done),
                                      text=summarise_report(plan, tuple(done), completed=False,
                                                            paused=True),
                                      reason=NEEDS_APPROVAL_REASON, paused=True,
                                      paused_step=step.index)
                if not outcome.ok:
                    self._lifecycle("plan.step_failed",
                                    {"goal": plan.goal, "step": step.index + 1,
                                     "summary": outcome.summary, "failure": outcome.failure})
                    return PlanReport(completed=False, outcomes=tuple(done),
                                      text=summarise_report(plan, tuple(done), completed=False),
                                      reason=outcome.failure or "step_failed")

            self._lifecycle("plan.completed", {"goal": plan.goal, "steps": total})
            return PlanReport(completed=True, outcomes=tuple(done),
                              text=summarise_report(plan, tuple(done), completed=True))
        except Exception as exc:  # total boundary
            log.warning("planner.run_failed: %r", exc)
            return PlanReport(completed=False, text="Plan execution failed unexpectedly.",
                              reason="runner_error")

    def _run_and_verify_step(self, step: PlanStep, task_id: str) -> StepOutcome:
        """Run the step, then apply reliability — a safe single retry for a TRANSIENT failure of an
        idempotent step, then evidence-based verification. A successful-but-unverified step is a
        failure (never assume success), so the plan halts rather than building on it."""
        outcome = self._safe_step(step, task_id)
        if outcome.approval_id:
            return StepOutcome(index=step.index, ok=False, verified=False,
                               summary=outcome.summary or f"step {step.index + 1} needs approval",
                               failure=NEEDS_APPROVAL_REASON, approval_id=outcome.approval_id,
                               content=outcome.content)
        if not outcome.ok:
            kind = classify_failure(outcome.summary)
            if kind is FailureKind.TRANSIENT and self._is_retriable(step.tool or ""):
                self._status(f"Step {step.index + 1} hit a transient error — retrying once…")
                outcome = self._safe_step(step, task_id)
            if not outcome.ok:
                kind = classify_failure(outcome.summary)
                return StepOutcome(index=step.index, ok=False, summary=outcome.summary,
                                   verified=False,
                                   failure=f"{kind.value}/{recovery_for(kind).value}")
        v = verify_step(ok=True, expected_evidence=step.expected_evidence, summary=outcome.summary)
        return StepOutcome(index=step.index, ok=v.verified, summary=outcome.summary,
                           verified=v.verified, content=outcome.content,
                           failure=("" if v.verified else f"unverified ({v.note})"))

    def _run_and_verify_approved(self, step: PlanStep, task_id: str, approval_id: str) -> StepOutcome:
        """The resumed step: spend the owner's approval on exactly this action."""
        outcome = self._safe_approved(step, task_id, approval_id)
        if not outcome.ok:
            kind = classify_failure(outcome.summary)
            return StepOutcome(index=step.index, ok=False, summary=outcome.summary, verified=False,
                               failure=f"{kind.value}/{recovery_for(kind).value}")
        v = verify_step(ok=True, expected_evidence=step.expected_evidence, summary=outcome.summary)
        return StepOutcome(index=step.index, ok=v.verified, summary=outcome.summary,
                           verified=v.verified, content=outcome.content,
                           failure=("" if v.verified else f"unverified ({v.note})"))

    def _safe_step(self, step: PlanStep, task_id: str) -> ToolOutcome:
        try:
            return self._run_tool(step.tool or "", step.arguments, task_id)
        except Exception as exc:
            log.warning("planner.step_failed: step=%s error=%r", step.index, exc)
            return ToolOutcome(ok=False, summary=f"error: {exc!r}")

    def _safe_approved(self, step: PlanStep, task_id: str, approval_id: str) -> ToolOutcome:
        try:
            return self._run_approved(step.tool or "", step.arguments, task_id, approval_id)  # type: ignore[misc]
        except Exception as exc:
            log.warning("planner.approved_step_failed: step=%s error=%r", step.index, exc)
            return ToolOutcome(ok=False, summary=f"error: {exc!r}")
