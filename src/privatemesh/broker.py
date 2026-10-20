"""The broker — the single door every skill call goes through.

New in PrivateMesh (the prior assistant enforced its gate inside an agent-SDK hook; this
repo has no such SDK, so the gate had to become an actual chokepoint in its own code).

One entry point: `Broker.invoke(task_id, skill, arguments)`.

  1. resolve the skill (an unknown name is denied and audited, never guessed at);
  2. `policy.evaluate` — the deterministic decision table, on this skill with these arguments,
     under the owner's current grants;
  3. DENY -> audit and return, nothing ran;
     NEEDS_APPROVAL -> open an `ApprovalRequest` bound to (task, skill, exact args, time) and
     return; `decide()` is what later spends the owner's answer, exactly once;
     ALLOW -> run it, gated by the `skills.guarded` wrapper so an executor cannot run any other way;
  4. record ONE `PolicyRecord` per attempt to the hash-chained audit — decision, whether it was
     approved, whether it ran, whether it worked, how long it took. A failed audit write raises:
     an action that cannot be receipted is not reported as done.

`record_model_saw` is the other half of the promise: per turn, the exact approved memory ids that
left the machine for the model.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

from .approval import ApprovalError, ApprovalStore
from .audit import AuditError, AuditLog, PolicyRecord, canonical
from .grants import GrantStore
from .policy import Decision, PolicyResult, evaluate
from .skills import SkillRegistry, ToolResult, _broker_context

log = logging.getLogger(__name__)

EXECUTED, NEEDS_APPROVAL, DENIED = "executed", "needs_approval", "denied"
UNKNOWN_SKILL_REASON = "skill_unknown"
INTENT_EVENT, DECISION_EVENT = "policy.intent", "policy.decision"


class UnevidencedExecution(AuditError):
    """The skill RAN, and the receipt that completes its record could not be written.

    A distinct class because it is a distinct fact, and the distinction is the whole product. An
    `AuditError` raised on the way IN means nothing happened. This one means something happened
    and the proof of it did not — for `send_message` a row is already in the outbox; for a real
    external executor it is an irreversible act with no receipt. Reporting that as an ordinary
    failure would be the lie this repo exists to refuse, so it says exactly what happened and
    carries the intent receipt's id so an operator can find the reservation it belongs to.
    """

    def __init__(self, message: str, *, intent: dict, task_id: str, skill: str) -> None:
        super().__init__(message)
        self.intent = intent
        self.intent_id = intent.get("id")
        self.task_id = task_id
        self.skill = skill


@dataclass(frozen=True)
class Invocation:
    """What happened to one skill call. `status` is the caller-facing verdict; the rest is the
    receipt."""

    task_id: str
    skill: str
    arguments: dict
    status: str                     # executed | needs_approval | denied
    decision: str                   # allow | needs_approval | deny
    reason: str
    ok: bool | None = None          # None = never ran
    content: str = ""
    error: str | None = None
    approval_id: str | None = None
    risk: str = ""
    tier: str = ""
    sensitive: bool = False
    duration_ms: int | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ran(self) -> bool:
        return self.status == EXECUTED


class Broker:
    def __init__(self, registry: SkillRegistry, audit: AuditLog, *,
                 approvals: ApprovalStore | None = None,
                 grants: GrantStore | None = None,
                 interactive: bool = True) -> None:
        self.registry = registry
        self.audit = audit
        self.approvals = approvals or ApprovalStore()
        self.grants = grants or GrantStore()
        # `interactive=False` models an unattended run: policy turns every NEEDS_APPROVAL into a
        # hard DENY, so nothing waits forever on a human who is not there.
        self.interactive = interactive

    # ---- the one entry point ------------------------------------------------------------------
    def invoke(self, task_id: str, skill: str, arguments: dict | None = None) -> Invocation:
        """Ask to run `skill` with `arguments` on behalf of `task_id`. Never raises for a policy
        outcome; DOES raise if the audit write fails (see module docstring)."""
        arguments = dict(arguments or {})
        tool = self.registry.get(skill)
        if tool is None:
            self.audit.record("skill.unknown", task_id=task_id,
                              detail={"skill": skill, "arguments": arguments})
            return Invocation(task_id=task_id, skill=skill, arguments=arguments, status=DENIED,
                              decision=Decision.DENY.value, reason=UNKNOWN_SKILL_REASON,
                              ok=None, error=f"unknown skill '{skill}'")

        missing = [k for k in ((tool.parameters or {}).get("required") or ()) if arguments.get(k) in (None, "")]
        if missing:
            # A call that cannot run must never reach the owner as an approval request: approving
            # an empty `send_message` would spend the owner's trust on nothing.
            self.audit.record("skill.invalid_arguments", task_id=task_id,
                              detail={"skill": skill, "missing": missing})
            return Invocation(task_id=task_id, skill=skill, arguments=arguments, status=DENIED,
                              decision=Decision.DENY.value,
                              reason=f"missing required argument(s): {', '.join(missing)}",
                              ok=None, error=f"{skill} needs {', '.join(missing)}")

        policy = evaluate(tool, arguments, interactive=self.interactive,
                          autonomy=self.grants.policy())

        if policy.decision is Decision.DENY:
            self._record(task_id, skill, tool.gate_name, policy, approved=None, executed=False,
                         ok=None, duration_ms=None, detail="denied before execution")
            return Invocation(task_id=task_id, skill=skill, arguments=arguments, status=DENIED,
                              decision=policy.decision.value, reason=policy.reason, ok=None,
                              risk=policy.risk.name, tier=policy.tier.value,
                              sensitive=policy.sensitive, error=policy.reason)

        if policy.decision is Decision.NEEDS_APPROVAL:
            # Raising an approval is itself a durable state change (it becomes recoverable and
            # spendable), so it gets the same reserve-then-act ordering as a skill.
            intent = self._reserve(task_id, skill, tool.gate_name, policy, approved=None,
                                   detail="reserved before raising an approval")
            req = self.approvals.request(task_id, skill, arguments)
            self.audit.record("approval.requested", task_id=task_id, detail=req.as_dict())
            self._complete(intent, task_id, skill, tool.gate_name, policy, approved=None,
                           executed=False, ok=None, duration_ms=None,
                           detail=f"approval {req.id} requested")
            return Invocation(task_id=task_id, skill=skill, arguments=arguments,
                              status=NEEDS_APPROVAL, decision=policy.decision.value,
                              reason=policy.reason, ok=None, approval_id=req.id,
                              risk=policy.risk.name, tier=policy.tier.value,
                              sensitive=policy.sensitive)

        # The receipt is reserved BEFORE the skill runs, and a reservation that cannot be written
        # stops the action — see `_reserve`. This is the ordering the review called out: the
        # earlier version executed first and receipted after, so a failed write left a real
        # effect with no record of it.
        intent = self._reserve(task_id, skill, tool.gate_name, policy, approved=None,
                               detail="reserved before execution (allow)")
        res, duration_ms = self._execute(tool, arguments)
        self._complete(intent, task_id, skill, tool.gate_name, policy, approved=None,
                       executed=True, ok=res.ok, duration_ms=duration_ms, detail=_summarise(res))
        return Invocation(task_id=task_id, skill=skill, arguments=arguments, status=EXECUTED,
                          decision=policy.decision.value, reason=policy.reason, ok=res.ok,
                          content=res.content, error=res.error, risk=policy.risk.name,
                          tier=policy.tier.value, sensitive=policy.sensitive,
                          duration_ms=duration_ms, meta=dict(res.meta or {}))

    # ---- the owner's answer -------------------------------------------------------------------
    def decide(self, approval_id: str, *, approve: bool) -> Invocation:
        """Spend (or refuse) a pending approval. Raises `ApprovalError` if the approval is unknown,
        already decided, already used, or — on approve — no longer permitted by the current grants.

        The action that runs is the one that was REQUESTED: the arguments come from the stored
        `ApprovalRequest`, never from this call. A caller cannot approve one action and run another."""
        req = self.approvals.get(approval_id)
        if req is None:
            raise ApprovalError(f"no such approval '{approval_id}'")

        if not approve:
            self.approvals.deny(approval_id)
            self.audit.record("approval.denied", task_id=req.task_id,
                              detail={"approval_id": approval_id, "skill": req.tool,
                                      "arguments": req.arguments})
            self._record(req.task_id, req.tool, _gate_for(self.registry, req.tool),
                         _result_for(self.registry, req.tool, req.arguments, self.grants),
                         approved=False, executed=False, ok=None, duration_ms=None,
                         detail=f"owner denied approval {approval_id}")
            return Invocation(task_id=req.task_id, skill=req.tool, arguments=req.arguments,
                              status=DENIED, decision=Decision.NEEDS_APPROVAL.value,
                              reason="the owner denied this action", ok=None,
                              approval_id=approval_id)

        # Revocation takes effect immediately: a grant set to `deny` after the request was raised
        # must stop the action, even though the owner is now saying yes.
        tool = self.registry.get(req.tool)
        if tool is None:
            raise ApprovalError(f"skill '{req.tool}' is no longer registered")
        current = evaluate(tool, req.arguments, interactive=self.interactive,
                           autonomy=self.grants.policy())
        if current.decision is Decision.DENY:
            self.approvals.deny(approval_id)
            self.audit.record("approval.revoked", task_id=req.task_id,
                              detail={"approval_id": approval_id, "skill": req.tool,
                                      "reason": current.reason})
            self._record(req.task_id, req.tool, tool.gate_name, current, approved=False,
                         executed=False, ok=None, duration_ms=None,
                         detail=f"refused at approval time: {current.reason}")
            return Invocation(task_id=req.task_id, skill=req.tool, arguments=req.arguments,
                              status=DENIED, decision=Decision.DENY.value, reason=current.reason,
                              ok=None, approval_id=approval_id)

        self.approvals.approve(approval_id)
        try:
            # The single-use gate. Raises if this approval was already consumed, or is being
            # presented for a different task/skill/arguments than the ones it was raised for.
            self.approvals.consume(approval_id, task_id=req.task_id, tool=req.tool,
                                   arguments=req.arguments)
        except ApprovalError as exc:
            self.audit.record("approval.replay_refused", task_id=req.task_id,
                              detail={"approval_id": approval_id, "skill": req.tool,
                                      "error": str(exc)})
            raise
        self.audit.record("approval.granted", task_id=req.task_id,
                          detail={"approval_id": approval_id, "skill": req.tool,
                                  "arguments": req.arguments})

        # Same ordering as the allow path: the reservation lands before the skill, and the
        # approval has already been spent by this point, so an unwritable reservation must stop
        # the action rather than leave a spent approval next to an unreceipted effect.
        intent = self._reserve(req.task_id, req.tool, tool.gate_name, current, approved=True,
                               detail=f"reserved before executing approval {approval_id}")
        res, duration_ms = self._execute(tool, req.arguments)
        self._complete(intent, req.task_id, req.tool, tool.gate_name, current, approved=True,
                       executed=True, ok=res.ok, duration_ms=duration_ms,
                       detail=f"approval {approval_id}: {_summarise(res)}")
        return Invocation(task_id=req.task_id, skill=req.tool, arguments=req.arguments,
                          status=EXECUTED, decision=Decision.NEEDS_APPROVAL.value,
                          reason="approved by the owner", ok=res.ok, content=res.content,
                          error=res.error, approval_id=approval_id, risk=current.risk.name,
                          tier=current.tier.value, sensitive=current.sensitive,
                          duration_ms=duration_ms, meta=dict(res.meta or {}))

    # ---- what the model saw -------------------------------------------------------------------
    def record_model_saw(self, task_id: str, memory_ids: list[int], *, note: str = "",
                         phase: str = "", tier: str = "", messages=None,
                         memory_hashes=None) -> dict:
        """Record what ONE model call is about to send. This is the answer to the one question
        every judge asks about a private assistant doing hosted inference, so it is an audit
        event rather than a log line.

        Called immediately before each call rather than once per turn. A turn-level "these ids
        were recalled" event is a claim about intent; the thing that leaves the machine is the
        rendered prompt of a specific call, and those two drift apart the moment a prompt builder
        changes, a retry fires, or extra context is attached. So each call records its phase
        (intent / plan / repair / answer), the ordered memory ids, a digest of each memory's
        rendered text, and a digest of the exact outbound message array.

        `messages` must be the SANITIZED messages — the ones actually posted — so the digest binds
        to the bytes the provider receives and not to the pre-redaction draft."""
        detail: dict = {"memory_ids": [int(i) for i in memory_ids], "note": note}
        if phase:
            detail["phase"] = phase
        if tier:
            detail["tier"] = tier
        if messages is not None:
            detail["message_count"] = len(messages)
            detail["messages_sha256"] = hashlib.sha256(
                canonical(list(messages)).encode("utf-8")).hexdigest()
        if memory_hashes:
            detail["memory_hashes"] = list(memory_hashes)
        return self.audit.record("model_saw", task_id=task_id, detail=detail)

    # ---- internals ----------------------------------------------------------------------------
    def _execute(self, tool, arguments: dict) -> tuple[ToolResult, int]:
        """Run a skill's executor. The `_broker_context` is what makes the call legal: outside it,
        `skills.guarded` refuses. Never raises — a failing skill is a ToolResult."""
        start = time.perf_counter()
        try:
            with _broker_context():
                result = tool.fn(arguments)
        except Exception as exc:  # a skill must not be able to take the turn down
            log.warning("broker.skill_raised: skill=%s error=%r", tool.name, exc)
            result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        duration_ms = int((time.perf_counter() - start) * 1000)
        if not isinstance(result, ToolResult):
            result = ToolResult(ok=False, error=f"skill '{tool.name}' returned {type(result).__name__}, not ToolResult")
        return result, duration_ms

    def _record(self, task_id: str, skill: str, gate_name: str, policy: PolicyResult, *,
                approved: bool | None, executed: bool, ok: bool | None,
                duration_ms: int | None, detail: str) -> dict:
        return self.audit.record(DECISION_EVENT, task_id=task_id, detail=PolicyRecord(
            task_id=task_id, tool=skill, gate_name=gate_name, tier=policy.tier.value,
            risk=policy.risk.name, sensitive=policy.sensitive, decision=policy.decision.value,
            reason=policy.reason, approved=approved, executed=executed, ok=ok,
            duration_ms=duration_ms, detail=detail).as_row())

    def _reserve(self, task_id: str, skill: str, gate_name: str, policy: PolicyResult, *,
                 approved: bool | None, detail: str) -> dict:
        """Write the INTENT receipt and return it. Raises `AuditError` if it cannot be written.

        The caller must not run the skill when this raises. That asymmetry is the fix: a missing
        receipt BEFORE an action is a reason not to act, and only a missing receipt AFTER one is
        something to report loudly (`_complete`)."""
        return self.audit.record(INTENT_EVENT, task_id=task_id, detail=PolicyRecord(
            task_id=task_id, tool=skill, gate_name=gate_name, tier=policy.tier.value,
            risk=policy.risk.name, sensitive=policy.sensitive, decision=policy.decision.value,
            reason=policy.reason, approved=approved, executed=False, ok=None, duration_ms=None,
            detail=detail).as_row())

    def _complete(self, intent: dict, task_id: str, skill: str, gate_name: str,
                  policy: PolicyResult, *, approved: bool | None, executed: bool,
                  ok: bool | None, duration_ms: int | None, detail: str) -> dict:
        """Transition a reservation to executed/failed by appending its completion.

        An append-only chain cannot rewrite the intent row, so the transition is a second event
        that names the first — the pair is the record, and neither half is meaningful alone.
        A failure HERE is the one case that must not look like an ordinary audit error: the
        action already happened."""
        try:
            return self._record(task_id, skill, gate_name, policy, approved=approved,
                                executed=executed, ok=ok, duration_ms=duration_ms,
                                detail=f"{detail} [intent #{intent.get('id')}]")
        except AuditError as exc:
            raise UnevidencedExecution(
                f"skill '{skill}' RAN but its completion receipt could not be written "
                f"(intent receipt #{intent.get('id')} at {intent.get('ts')} exists; the action "
                f"itself is not recorded as done): {exc}",
                intent=intent, task_id=task_id, skill=skill) from exc


def _summarise(res: ToolResult) -> str:
    if res.ok:
        return (res.content or "")[:200]
    return f"failed: {res.error or 'no error given'}"


def _gate_for(registry: SkillRegistry, skill: str) -> str:
    tool = registry.get(skill)
    return tool.gate_name if tool else ""


def _result_for(registry: SkillRegistry, skill: str, arguments: dict,
                grants: GrantStore) -> PolicyResult:
    """Re-evaluate for an audited decision about a skill that may no longer be registered."""
    tool = registry.get(skill)
    if tool is None:
        from .policy import Risk, Tier
        return PolicyResult(Decision.DENY, f"skill '{skill}' is no longer registered",
                            Risk.BLOCKED, Tier.ACT, True)
    return evaluate(tool, arguments, interactive=True, autonomy=grants.policy())
