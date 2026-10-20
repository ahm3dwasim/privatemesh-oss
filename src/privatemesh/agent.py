"""The agent loop — intent, recall, plan, execute through the broker, answer.

The model does the language work; the kernel does the deciding. One turn:

  1. **FAST tier** — classify the request: a direct question to answer, or a task to plan, plus
     the search terms to recall memories with.
  2. **Recall** — approved, valid memories only (see `memory.recall`), and the ids that were sent
     are written to the audit as a `model_saw` event before the model sees them.
  3. **REASON tier** plans the task, escalating to **DEEP** only when the intent says it is
     multi-step; a plan that will not parse gets exactly ONE repair retry, then the turn declines
     to act rather than flailing.
  4. **PlanRunner** executes step by step, every step through `broker.invoke`, so the policy table
     and the approval binding apply to each call.
  5. The first step the broker refuses to run without the owner **pauses the whole run**, and the
     turn is stored. `resume` picks it up at that same step once the approval is spent — the
     remaining steps still go through the broker, so a plan approval never launders a later one.
  6. **REASON tier** writes the answer from what actually happened, not from what was planned.

Turns are held in memory keyed by turn id. A restart forgets them, which fails closed: a paused
run that is gone cannot be resumed, and nothing executes without a fresh approval.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from . import memory as mem
from .approval import PENDING
from .audit import AuditLog
from .broker import Broker, Invocation
from .llm import Tier, TokenFactory
from .redact import egress_sanitize
from .planner import (INVALID_PLAN_REASON, NO_PLAN_REASON, Plan, PlanReport, PlanRunner,
                      ToolOutcome, build_plan_prompt, parse_plan, render_plan, strip_reasoning,
                      validate)
from .skills import SkillRegistry
from .turnstore import TurnStore

log = logging.getLogger(__name__)

ANSWERED, NEEDS_APPROVAL, HALTED, ERROR = "answered", "needs_approval", "halted", "error"
MAX_MEMORIES = 6

INTENT_SYSTEM = (
    "You route a personal assistant's turn. Reply with ONLY a JSON object, no prose:\n"
    '{"action": "answer" | "task", "query": "<keywords to search the owner\'s memories>", '
    '"steps": <integer, your guess at how many tool steps the task needs; 0 for a question>}\n'
    'Use "answer" when the request can be met by replying directly. Use "task" when it needs a '
    "tool or skill to run."
)

PLAN_REPAIR = (
    "That was not valid JSON. Reply with ONLY the JSON object described above — no prose, no "
    "markdown fence, no trailing commentary."
)

RECALL_NOTE = (
    "The owner has approved these memories about themselves. Use them if they are relevant, and "
    "do not claim to know anything they do not say."
)
FINAL_SYSTEM = (
    "You are PrivateMesh, a personal assistant that acts only with the owner's permission. Write "
    "the reply to the owner from the run's actual results — say plainly what was done, what is "
    "waiting on them, and what failed. Never claim an action succeeded if the results do not show "
    "it."
)


@dataclass
class Turn:
    """One conversation turn and its receipt."""

    id: str
    task_id: str
    text: str
    status: str = ANSWERED
    answer: str = ""
    plan: Plan | None = None
    report: PlanReport | None = None
    approval_id: str | None = None
    paused_step: int | None = None
    memory_ids: list[int] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    reason: str = ""
    tiers: dict = field(default_factory=dict)
    # The exact rendered lines that carry the memories into the prompt, positioned to match
    # `memories`. Kept so a receipt can hash what the model was SHOWN, not the id it was fetched
    # under — a per-memory content hash rather than a per-memory claim.
    memory_lines: list[str] = field(default_factory=list)

    def receipts(self) -> list[dict]:
        if self.report is None:
            return []
        return [{"step": o.index + 1, "ok": o.ok, "summary": o.summary, "verified": o.verified,
                 "failure": o.failure, "approval_id": o.approval_id or None} for o in self.report.outcomes]

    def to_state(self) -> dict:
        """The turn as plain data, for `TurnStore`. Every field a resumed turn needs is here:
        the plan and the outcomes so far (so a resume continues at the same step), the approval it
        paused on, and the recalled memories the answer will be written from."""
        return {"id": self.id, "task_id": self.task_id, "text": self.text, "status": self.status,
                "answer": self.answer, "reason": self.reason, "approval_id": self.approval_id,
                "paused_step": self.paused_step, "plan": self.plan, "report": self.report,
                "memory_ids": self.memory_ids, "memories": self.memories,
                "memory_lines": self.memory_lines, "tiers": self.tiers}

    @classmethod
    def from_state(cls, state: dict) -> "Turn":
        return cls(id=state["id"], task_id=state["task_id"], text=state["text"],
                   status=state.get("status", ANSWERED), answer=state.get("answer", ""),
                   plan=state.get("plan"), report=state.get("report"),
                   approval_id=state.get("approval_id"), paused_step=state.get("paused_step"),
                   memory_ids=list(state.get("memory_ids") or []),
                   memories=list(state.get("memories") or []),
                   reason=state.get("reason", ""), tiers=dict(state.get("tiers") or {}),
                   memory_lines=list(state.get("memory_lines") or []))

    def as_dict(self) -> dict:
        return {"turn_id": self.id, "task_id": self.task_id, "text": self.text,
                "status": self.status, "answer": self.answer, "reason": self.reason,
                "approval_id": self.approval_id, "paused_step": self.paused_step,
                "memory_ids": self.memory_ids, "tiers": self.tiers,
                "plan": render_plan(self.plan) if self.plan else None,
                "receipts": self.receipts()}


def _chat_text(llm: TokenFactory, messages: list[dict], tier: Tier, **kw) -> str:
    out = llm.chat(messages, tier=tier, **kw)
    try:
        return str(out["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError):
        return ""


def _json_object(text: str) -> dict | None:
    """Parse a JSON object out of model text, tolerating reasoning and a fence.

    Order matters: the `<think>` block goes first, because a fence that appears inside reasoning
    is not a fence around the answer."""
    if not text:
        return None
    t = strip_reasoning(text).strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    try:
        data = json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(t[start:end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


class Agent:
    def __init__(self, llm: TokenFactory, broker: Broker, conn, registry: SkillRegistry,
                 audit: AuditLog, turns: "TurnStore | None" = None) -> None:
        self.llm = llm
        self.broker = broker
        self.conn = conn
        self.registry = registry
        self.audit = audit
        # Optional durability. With a store, a paused turn survives a restart; without one the
        # agent behaves exactly as before (in-memory, fails closed on restart).
        self.turns = turns
        self._turns: dict[str, Turn] = {}
        self._by_approval: dict[str, str] = {}
        self.runner = PlanRunner(
            run_tool=self._run_tool,
            approve_plan=self._approve_plan,
            run_approved=self._run_approved,
            is_retriable=self.registry.is_retriable,
        )

    # ---- entry points ---------------------------------------------------------------------
    def run(self, text: str, *, task_id: str | None = None) -> Turn:
        """Handle one request. Returns a Turn — `needs_approval` when it is waiting on the owner."""
        task_id = task_id or uuid.uuid4().hex
        turn = Turn(id=uuid.uuid4().hex, task_id=task_id, text=text)
        self._turns[turn.id] = turn
        try:
            self._run_turn(turn)
        except Exception as exc:  # the loop must answer, even when something breaks
            log.warning("agent.turn_failed: %r", exc)
            turn.status = ERROR
            turn.reason = f"{type(exc).__name__}: {exc}"
            turn.answer = "Something went wrong handling that request, so nothing was run."
        self._persist(turn)
        return turn

    def resume(self, turn_id: str, *, decided: bool = False) -> Turn:
        """Continue a paused turn.

        `decided=True` means the caller IS the decision (the approval endpoint); otherwise the
        approval must already have been decided, so a plain "continue" cannot approve anything by
        itself. That is the difference between resuming a run and waving it through."""
        turn = self._get(turn_id)
        if turn is None:
            raise KeyError(f"no such turn '{turn_id}'")
        if turn.status != NEEDS_APPROVAL or turn.report is None or turn.plan is None:
            return turn
        approval_id = turn.approval_id
        if not decided:
            req = self.broker.approvals.get(approval_id) if approval_id else None
            if req is None or req.status == PENDING:
                return turn          # still waiting on the owner — nothing to resume
        turn.approval_id, turn.paused_step = None, None
        report = self.runner.run(turn.plan, task_id=turn.task_id,
                                 outcomes=turn.report.outcomes, pending_approval=approval_id)
        self._settle(turn, report)
        return turn

    def resume_for_approval(self, approval_id: str) -> Turn | None:
        """Resume the turn that is waiting on exactly this approval. None otherwise — a turn that
        already moved on, or an approval no turn raised, is not resumed by a stray decision.

        The approval store is consulted when this process has no memory of the turn: after a
        restart the linkage lives in the `turn_id` column, which is what makes a decision the
        owner made before the restart still resume the run it was made for."""
        turn_id = self._by_approval.get(approval_id)
        if turn_id is None:
            turn_id = self.broker.approvals.turn_for(approval_id)
        if turn_id is None:
            return None
        turn = self._get(turn_id)
        if turn is None or turn.status != NEEDS_APPROVAL or turn.approval_id != approval_id:
            return None
        self._by_approval[approval_id] = turn_id
        return self.resume(turn_id, decided=True)

    def turn(self, turn_id: str) -> Turn | None:
        return self._get(turn_id)

    def _get(self, turn_id: str) -> Turn | None:
        """The in-process turn, or the stored one if this process has not seen it yet."""
        turn = self._turns.get(turn_id)
        if turn is not None or self.turns is None:
            return turn
        state = self.turns.load(turn_id)
        if state is None:
            return None
        turn = Turn.from_state(state)
        self._turns[turn_id] = turn
        if turn.approval_id:
            self._by_approval[turn.approval_id] = turn_id
        return turn

    def _persist(self, turn: Turn) -> None:
        if self.turns is None:
            return
        try:
            self.turns.save(turn.to_state())
        except Exception as exc:      # durability must never take a turn down
            log.warning("agent.turn_persist_failed: %r", exc)

    # ---- the turn -------------------------------------------------------------------------
    def _run_turn(self, turn: Turn) -> None:
        intent = self._intent(turn.text, turn)
        query = str(intent.get("query") or turn.text)
        hits = mem.recall(self.conn, query, k=MAX_MEMORIES)
        turn.memory_ids = [h.id for h in hits]
        turn.memories = [{"id": h.id, "type": h.type, "text": h.text} for h in hits]
        # No turn-level `model_saw` here any more. Recall is not what leaves the machine — the
        # rendered prompt of each individual call is — so the receipt is emitted per call, by
        # `_model_call`, immediately before that call, carrying that call's ids and hashes.

        if str(intent.get("action", "answer")).lower() != "task":
            turn.answer = self._answer(turn, question=turn.text)
            turn.status = ANSWERED
            return

        try:
            steps_hint = int(intent.get("steps") or 0)
        except (TypeError, ValueError):
            steps_hint = 0
        tier = Tier.DEEP if steps_hint > 1 else Tier.REASON
        plan = self._plan(turn, tier)
        turn.plan = plan

        if plan is None:
            turn.status = HALTED
            turn.reason = NO_PLAN_REASON
            turn.answer = ("I couldn't turn that into a plan I'd be willing to run, so I did "
                           "nothing. Could you say what you'd like done in one more step?")
            return
        verdict = validate(plan, self.registry.names())
        if not verdict.ok:
            turn.status = HALTED
            turn.reason = INVALID_PLAN_REASON
            turn.answer = ("That plan didn't pass validation, so nothing ran: "
                           + "; ".join(verdict.errors))
            return

        self.audit.record("plan.proposed", task_id=turn.task_id,
                          detail={"goal": plan.goal, "steps": len(plan.steps),
                                  "plan": render_plan(plan)})
        self._settle(turn, self.runner.run(plan, task_id=turn.task_id))

    def _settle(self, turn: Turn, report: PlanReport) -> None:
        """Record the plan's outcome. These are written HERE rather than through the runner's
        lifecycle sink, because that sink is deliberately swallow-on-error (a status callback must
        never break a plan) and a plan receipt must not be swallowed."""
        turn.report = report
        if report.paused:
            self.audit.record("plan.paused", task_id=turn.task_id,
                              detail={"step": report.paused_step, "reason": report.reason})
        elif report.completed:
            self.audit.record("plan.completed", task_id=turn.task_id,
                              detail={"steps": len(report.outcomes)})
        else:
            self.audit.record("plan.halted", task_id=turn.task_id,
                              detail={"reason": report.reason,
                                      "step": (report.outcomes[-1].index + 1) if report.outcomes else None})
        if report.paused:
            step = next((o for o in report.outcomes if o.approval_id), None)
            turn.status = NEEDS_APPROVAL
            turn.reason = report.reason
            turn.approval_id = step.approval_id if step else None
            turn.paused_step = report.paused_step
            if turn.approval_id:
                self._by_approval[turn.approval_id] = turn.id
                # The other direction of the link, and the one that survives a restart: the
                # approval row names the turn it paused, so recovery can find both halves.
                self.broker.approvals.link_turn(turn.approval_id, turn.id)
            turn.answer = report.text
            self._persist(turn)
            return
        turn.status = ANSWERED if report.completed else HALTED
        turn.reason = report.reason
        turn.answer = self._answer(turn, question=turn.text, run_text=report.text)
        self._persist(turn)

    # ---- model calls ----------------------------------------------------------------------
    def _model_call(self, turn: Turn, messages: list[dict], tier: Tier, phase: str, **kw) -> str:
        """One model call and its receipt, in that order: sanitize, record, send.

        The digest is taken over the SANITIZED array — the bytes the provider will actually
        receive — so the receipt can never describe a cleaner prompt than the one that left.
        `TokenFactory.chat` runs the sanitizer again as the chokepoint; on already-clean input
        that second pass is a no-op, which is exactly what makes hashing here trustworthy."""
        safe, redactions = egress_sanitize(messages)
        if redactions:
            # Count only, never the value: the thing that was removed is the thing that must not
            # be written down.
            self.audit.record("egress.redacted", task_id=turn.task_id,
                              detail={"count": redactions, "phase": phase, "tier": tier.value})
        self.broker.record_model_saw(
            turn.task_id, turn.memory_ids, note=f"{phase}: {turn.text[:60]}", phase=phase,
            tier=tier.value, messages=safe, memory_hashes=self._memory_hashes(turn))
        return _chat_text(self.llm, safe, tier, task_id=turn.task_id, phase=phase, **kw)

    def _memory_hashes(self, turn: Turn) -> list[dict]:
        """One digest per memory, over the exact rendered line it appears as in the prompt."""
        if not turn.memories:
            return []
        return [{"id": m["id"],
                 "sha256": hashlib.sha256(line.encode("utf-8")).hexdigest()}
                for m, line in zip(turn.memories, turn.memory_lines)]

    def _intent(self, text: str, turn: Turn) -> dict:
        turn.tiers["intent"] = Tier.FAST.value
        raw = self._model_call(turn, [{"role": "system", "content": INTENT_SYSTEM},
                                      {"role": "user", "content": text}],
                               Tier.FAST, "intent", max_tokens=200)
        data = _json_object(raw)
        if not data or str(data.get("action", "")).lower() not in {"answer", "task"}:
            # An unreadable intent is not a reason to refuse: answer, which touches nothing.
            return {"action": "answer", "query": text, "steps": 0}
        return data

    def _plan(self, turn: Turn, tier: Tier) -> Plan | None:
        turn.tiers["plan"] = tier.value
        # The planner's instructions ARE the system prompt (the ported builder embeds the request);
        # the owner's approved memories go in with them, so the plan can use what the model is
        # allowed to know and nothing else.
        system = build_plan_prompt(turn.text, self.registry.all()) + "\n\n" + self._memory_block(turn)
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": turn.text}]
        # `response_format` asks the provider for a JSON object outright. It is a request, not a
        # guarantee — the strict parser below is still the gate, and the repair retry still backs
        # it up — but a reasoning model that is told the shape up front stops wrapping the plan in
        # prose it then has to be talked out of.
        raw = self._model_call(turn, messages, tier, "plan", max_tokens=900,
                               response_format={"type": "json_object"})
        plan = parse_plan(raw)
        if plan is not None:
            return plan
        # ONE repair retry — and only one; a model that cannot produce JSON twice will not
        # produce it on the fifth attempt, and the loop must not spend the owner's budget flailing.
        repair = messages + [{"role": "assistant", "content": raw or ""},
                             {"role": "user", "content": PLAN_REPAIR}]
        return parse_plan(self._model_call(turn, repair, tier, "repair", max_tokens=900,
                                           response_format={"type": "json_object"}))

    def _answer(self, turn: Turn, *, question: str, run_text: str = "") -> str:
        turn.tiers["answer"] = Tier.REASON.value
        parts = [f"Owner's request: {question}"]
        if run_text:
            parts.append(f"Run results:\n{run_text}")
        if turn.memories:
            parts.append(self._memory_block(turn))
        return self._model_call(turn,
                                [{"role": "system", "content": FINAL_SYSTEM},
                                 {"role": "user", "content": "\n\n".join(parts)}],
                                Tier.REASON, "answer", max_tokens=500).strip() or "(no reply)"

    def _memory_block(self, turn: Turn) -> str:
        turn.memory_lines = ([f"- [{m['id']}] ({m['type']}) {m['text']}" for m in turn.memories]
                             if turn.memories else [])
        if not turn.memory_lines:
            return RECALL_NOTE + "\n(no approved memories matched this request)"
        return RECALL_NOTE + "\n" + "\n".join(turn.memory_lines)

    # ---- the broker seam ------------------------------------------------------------------
    def _run_tool(self, skill: str, arguments: dict, task_id: str) -> ToolOutcome:
        inv = self.broker.invoke(task_id, skill, arguments)
        return _outcome(inv)

    def _run_approved(self, skill: str, arguments: dict, task_id: str, approval_id: str) -> ToolOutcome:
        try:
            inv = self.broker.decide(approval_id, approve=True)
        except Exception as exc:  # a refused/expired approval is a step failure, not a crash
            self.audit.record("approval.refused", task_id=task_id,
                              detail={"approval_id": approval_id, "skill": skill, "error": str(exc)})
            return ToolOutcome(ok=False, summary=f"approval refused: {exc}")
        return _outcome(inv)

    def _approve_plan(self, rendered: str) -> bool:
        """Stage approval. v0 SHOWS the plan and proceeds: the binding gate is per step, in the
        broker, which is the one the owner actually reads. Wiring a real stage confirmation here
        later changes nothing below it."""
        return True


def _outcome(inv: Invocation) -> ToolOutcome:
    if inv.status == NEEDS_APPROVAL:
        return ToolOutcome(ok=False, summary=f"{inv.skill} needs the owner's approval",
                           approval_id=inv.approval_id or "")
    if inv.status == "denied":
        return ToolOutcome(ok=False, summary=f"denied: {inv.reason}")
    return ToolOutcome(ok=bool(inv.ok), summary=inv.content if inv.ok else (inv.error or "failed"),
                       content=inv.content)
