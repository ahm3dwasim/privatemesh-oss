"""Proposals — how a generated skill asks for an effect without ever performing one.

The skill cannot send an email, update an event, or open a socket. What it can do is *return* a
description of an effect it believes should happen, and the trusted side decides. That inversion is
the whole safety story, and it only holds if the return value is treated as a request from an
untrusted party rather than as a result to be carried out.

Three checks stand between a returned proposal and an effect, in this order:

  1. the manifest's output schema — a surprise field means the result is not read at all;
  2. `manifest.proposes` — the effect kinds the owner granted when this identity was approved. A
     skill that proposes `calendar_update` while declaring only `email_reply` is refused HERE, and
     named, and the broker is never called;
  3. the broker's own policy — `broker.invoke` re-decides against the owner's current grants, so a
     proposal inside the manifest still does not execute. `email_draft` is a draft; the effect kind
     that actually leaves the machine is `send_message`, which needs an approval this module cannot
     manufacture.

`bindings` maps the product's effect vocabulary (`docs/API_V2.md`: `calendar_update`,
`email_reply`) onto broker skill names. It is explicit and supplied by the caller because the
mapping is a product decision, not something this module should infer from a string.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .manifest import SkillManifest
from .violations import SkillViolation

#: Effect kind -> broker skill. Only kinds that have a real broker skill are bound.
#:
#: `email_reply` maps to `email_draft`, which writes a LOCAL draft: proposing a reply is not
#: authority to send one. `calendar_update` has no binding yet because no calendar skill is
#: registered in this repo — and an unbound kind is REFUSED as unenforceable rather than quietly
#: dropped, so a manifest that declares it cannot be read as having been honoured. When a calendar
#: skill lands, add the binding here and the same manifest starts working; nothing else changes.
DEFAULT_BINDINGS: dict[str, str] = {"email_reply": "email_draft"}

#: A skill that returns a thousand proposals is not proposing, it is flooding.
MAX_PROPOSALS = 8


@dataclass(frozen=True)
class Proposal:
    """One requested effect, exactly as the skill asked for it. Never executed by this module."""

    kind: str
    arguments: dict = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "arguments": self.arguments, "reason": self.reason}


def extract(result: dict) -> list[Proposal]:
    """Pull proposals out of a skill's returned object. The field is optional — a skill that only
    computes (ranks meeting slots, say) proposes nothing, which is the common case."""
    raw = (result or {}).get("proposals") or []
    if not isinstance(raw, list):
        raise SkillViolation("proposal", f"'proposals' must be a list, got {type(raw).__name__}")
    if len(raw) > MAX_PROPOSALS:
        raise SkillViolation("proposal", f"{len(raw)} proposals returned; the limit is "
                                         f"{MAX_PROPOSALS}")
    out: list[Proposal] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SkillViolation("proposal", f"proposal {i} is {type(item).__name__}, not an object")
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind:
            raise SkillViolation("proposal", f"proposal {i} has no 'kind'")
        arguments = item.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise SkillViolation("proposal", f"proposal {i} 'arguments' must be an object")
        out.append(Proposal(kind, dict(arguments), str(item.get("reason") or "")))
    return out


def admit(manifest: SkillManifest, result: dict, *,
          bindings: dict[str, str] | None = None) -> list[Proposal]:
    """The manifest gate. Returns the proposals the owner's approval actually covers.

    Raises `SkillViolation(kind="proposal")` on anything outside it — a refusal that names the
    effect, so the report can say *which* overreach was attempted rather than "the skill misbehaved".
    """
    bindings = DEFAULT_BINDINGS if bindings is None else bindings
    allowed = set(manifest.proposes)
    admitted: list[Proposal] = []
    for proposal in extract(result):
        if proposal.kind not in allowed:
            raise SkillViolation(
                "proposal",
                f"skill '{manifest.name}' proposes {proposal.kind!r}, which its manifest does not "
                f"declare (allowed: {sorted(allowed) or 'nothing'})")
        if proposal.kind not in bindings:
            raise SkillViolation(
                "proposal",
                f"effect kind {proposal.kind!r} has no broker binding, so it cannot be admitted "
                f"(bound kinds: {sorted(bindings)})")
        admitted.append(proposal)
    return admitted


def submit(broker, task_id: str, manifest: SkillManifest, result: dict, *,
           bindings: dict[str, str] | None = None) -> list:
    """Admit, then hand each proposal to the broker. Returns one `Invocation` per proposal.

    The broker is called for every admitted proposal and its verdict is returned unchanged — this
    function adds no authority. A `needs_approval` here is the desired shape: the skill proposed,
    the broker paused, the owner decides.
    """
    bindings = DEFAULT_BINDINGS if bindings is None else bindings
    invocations = []
    for proposal in admit(manifest, result, bindings=bindings):
        invocations.append(broker.invoke(task_id, bindings[proposal.kind], proposal.arguments))
    return invocations
