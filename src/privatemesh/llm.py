"""Nebius Token Factory client + a cost-aware Nemotron router.

Tiers: FAST (Nano — intent, routing, short replies), REASON (Super — tool use, most turns),
DEEP (Ultra — multi-step planning only). Each call records tokens so the demo can show spend.

These tier models are *reasoning* models, and that is not a detail — it is the first thing that
bites. Left at their defaults they run a private reasoning pass that is billed against the same
`max_tokens` budget as the answer, so a small budget is spent entirely on thinking and the answer
arrives as `content: null`. The measured shape (2026-09-18) is a leading "\\n" on the content plus
a separate `reasoning_content` of a few hundred characters for a one-word reply.

So every call states its thinking intent explicitly, as `chat_template_kwargs.enable_thinking` in
the request body: off for FAST and REASON (REASON opts in per call), on for DEEP. A system-level
"/no_think" does NOT work — the flag has to be in the body. Every tier also carries a `max_tokens`
default sized for its job, overridable per call.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum

import httpx

from .config import Settings
from .redact import egress_sanitize
from .spend import SpendLedger

log = logging.getLogger(__name__)


class Tier(str, Enum):
    FAST = "fast"
    REASON = "reason"
    DEEP = "deep"


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    #: Reasoning is billed as completion tokens; when the provider breaks it out we keep it
    #: separate, because "the model spent 900 tokens thinking about a one-word answer" is the
    #: fact that explains an empty response and an unexpected bill.
    reasoning_tokens: int = 0
    #: How much reasoning text came back — its LENGTH only. The text itself is deliberately not
    #: kept anywhere: it is the model's private scratch, not an answer, and it must not reach a
    #: prompt, a memory or an audit detail.
    reasoning_chars: int = 0


class EmptyContent(RuntimeError):
    """The provider answered with nothing in `message.content`.

    Its own class because it has its own cause, and the cause is the actionable part: the
    reasoning pass is charged to the same `max_tokens` budget as the answer, so a budget that is
    too small is consumed by thinking and the answer never starts.
    """


@dataclass(frozen=True)
class TierDefaults:
    max_tokens: int
    enable_thinking: bool


#: What each tier sends when the caller does not override it. FAST is a router — short answers,
#: no thinking. REASON is the workhorse: no thinking unless the call asks for it. DEEP is the
#: multi-step planner, which is exactly the job thinking is for.
TIER_DEFAULTS: dict["Tier", TierDefaults] = {
    Tier.FAST: TierDefaults(max_tokens=256, enable_thinking=False),
    Tier.REASON: TierDefaults(max_tokens=2048, enable_thinking=False),
    Tier.DEEP: TierDefaults(max_tokens=8192, enable_thinking=True),
}


class TokenFactory:
    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 *, audit=None, spend: "SpendLedger | None" = None, boundary=None):
        if not settings.api_key:
            raise RuntimeError("NEBIUS_API_KEY is not set (see .env.example)")
        self.s = settings
        # Optional, and NOT a second sanitizer: `egress_sanitize` removes secrets, this refuses
        # protected personal content. They defend different things, so a caller attaching one is
        # not a reason to skip the other — see `chat`.
        self.boundary = boundary
        # Optional: when an audit log is attached, a redaction is receipted as it happens. The
        # sanitizer runs either way — the audit is evidence, not the enforcement.
        self.audit = audit
        # A real network client spends real money, so it gets the hard cap. An injected client
        # (tests, demo mode) is a fake transport and spends nothing — unless a ledger is passed.
        self.spend = spend if spend is not None else (None if client is not None else SpendLedger())
        self.http = client or httpx.Client(
            base_url=settings.base_url,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=120,
        )
        self.usage = Usage()

    def model_for(self, tier: Tier) -> str:
        m = {Tier.FAST: self.s.model_fast, Tier.REASON: self.s.model_reason,
             Tier.DEEP: self.s.model_deep}[tier]
        if not m:
            raise RuntimeError(f"no model configured for tier {tier.value} (set PM_MODEL_{tier.name})")
        return m

    def models(self) -> list[str]:
        r = self.http.get("models")
        r.raise_for_status()
        return sorted(m["id"] for m in r.json().get("data", []))

    def chat(self, messages: list[dict], tier: Tier = Tier.REASON, *, task_id: str = "",
             phase: str = "", max_tokens: int | None = None,
             enable_thinking: bool | None = None, model: str | None = None, **kw) -> dict:
        """POST one completion. The sanitizer below is not advisory — it is the last statement
        before the request body is built, and it either certifies the payload or raises.

        Putting it here rather than at each call site is the point: user text, planner repair
        transcripts, tool results and memory blocks all reach the provider through this one
        method, so a new call path cannot forget to scrub. `task_id`/`phase` label the audit
        event and are never forwarded in the request body.

        `max_tokens` and `enable_thinking` default per tier (see `TIER_DEFAULTS`) and are the only
        supported way to override them — a caller passing `chat_template_kwargs` through `**kw`
        replaces the whole object, which is an escape hatch, not the intended path.

        `model` overrides the tier's model for ONE call. The tier still supplies the defaults; only
        the model id changes. Its one caller is the skill reviewer, whose whole claim is that it is
        a different family from the generator: routing it through the tier table would silently
        make "a second model reviewed this" false the day someone edits PM_MODEL_DEEP."""
        model = model or self.model_for(tier)
        safe, redactions = egress_sanitize(messages)     # raises EgressBlocked -> nothing is sent
        if self.boundary is not None:
            # Substitute BEFORE checking, and in that order for a reason worth stating. A protected
            # fact must never reach a model, and there are two ways to be sure: never build the
            # prompt with it, or take it out. This is the second — the model is handed a placeholder
            # token and the engine keeps the value. The guard on the next line then runs on the
            # bytes that would actually be posted, so a masking bug is REFUSED rather than hidden:
            # if `scrub_for_model` missed a value, `guard_model_payload` raises on it and nothing
            # is sent. Masking and checking are separate steps precisely so one can catch the other.
            safe = self.boundary.scrub_for_model(safe)
            # The private boundary, on the sanitized payload — the bytes that would actually be
            # posted. It raises `boundary.EgressRefused` and, like the sanitizer, it runs before
            # the spend reservation and before the request body exists, so a refusal costs
            # nothing and sends nothing. Placed here rather than at each call site for the same
            # reason as the sanitizer: intent, extraction, research planning, the answer and any
            # retry all leave through this one method.
            self.boundary.guard_model_payload(safe, task_id=task_id, phase=phase)
        if redactions:
            # Count only. The value that was removed is exactly the thing that must not be
            # written down here, so the receipt records that it happened and how often.
            log.warning("llm.egress_redacted: %d secret(s) removed before the request (phase=%s)",
                        redactions, phase or "unlabelled")
            if self.audit is not None:
                self.audit.record("egress.redacted", task_id=task_id,
                                  detail={"count": redactions, "tier": tier.value,
                                          "phase": phase or "unlabelled", "model": model})
        d = TIER_DEFAULTS[tier]
        budget = d.max_tokens if max_tokens is None else int(max_tokens)
        thinking = d.enable_thinking if enable_thinking is None else bool(enable_thinking)
        body = {
            "model": model,
            "messages": safe,
            "max_tokens": budget,
            # The flag has to be in the body: a "/no_think" system message is ignored, and a
            # reasoning pass that eats the budget turns the answer into `content: null`.
            "chat_template_kwargs": {"enable_thinking": thinking},
            **kw,
        }
        # The spend cap is checked BEFORE anything is sent: SpendCapReached here means no request.
        reservation = (self.spend.reserve(model, len(json.dumps(safe)), budget)
                       if self.spend is not None else None)
        data: dict = {}
        try:
            r = self.http.post("chat/completions", json=body)
            r.raise_for_status()
            data = r.json()
        finally:
            if reservation is not None:
                self.spend.settle(reservation, model, (data or {}).get("usage"))
        self._account(data, model)
        return self._answer_text(data, model, budget, thinking)

    def _account(self, data: dict, model: str) -> None:
        """Fold one response's usage into the running total. A provider that reports no usage is
        recorded as having spent nothing rather than crashing the call that succeeded."""
        u = data.get("usage") or {}
        self.usage.calls += 1
        self.usage.prompt_tokens += u.get("prompt_tokens", 0) or 0
        self.usage.completion_tokens += u.get("completion_tokens", 0) or 0
        self.usage.by_model[model] = (self.usage.by_model.get(model, 0)
                                      + (u.get("total_tokens", 0) or 0))
        details = u.get("completion_tokens_details") or {}
        self.usage.reasoning_tokens += details.get("reasoning_tokens", 0) or 0

    def _answer_text(self, data: dict, model: str, budget: int, thinking: bool) -> dict:
        """Normalise the response's message in place, and hand the response back.

        Both jobs live here, and only here, so a call site cannot skip them: `content` is stripped
        of the provider's padding, and `reasoning_content` is REMOVED from the dict the caller
        receives. Keeping only its length is what makes "reasoning never travels" structural
        rather than a rule each caller has to remember — the text is not in the object they hold,
        so it cannot be echoed into a repair transcript, a memory or an audit detail."""
        choices = data.get("choices") or []
        if not choices:
            raise EmptyContent(f"{model} returned no choices at all")
        message = choices[0].get("message") or {}
        reasoning = message.pop("reasoning_content", None)
        chars = len(str(reasoning)) if reasoning else 0
        self.usage.reasoning_chars += chars
        content = message.get("content")
        if content is None:
            raise EmptyContent(
                f"{model} returned no answer text "
                f"(finish_reason={choices[0].get('finish_reason')!r}, reasoning={chars} chars): "
                f"the reasoning pass spent the {budget}-token budget before the answer began "
                f"(enable_thinking={thinking}) — raise max_tokens or send enable_thinking=false")
        message["content"] = (content if isinstance(content, str) else str(content)).strip()
        return data
