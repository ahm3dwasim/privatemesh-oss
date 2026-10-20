# Ported from the author's prior private assistant (Jarvis); adapted for PrivateMesh.
"""Verification + typed failure classification: "never assume success."

Ported from the prior assistant's `kernel/verify.py` essentially unchanged — it is pure, has no
dependencies, and the rule it encodes is the one the product sells: a step that *says* it
succeeded but produced no evidence is NOT accepted, and a failure is classified into a typed kind
with a recovery hint so the report says what went wrong rather than just that something did.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class FailureKind(str, Enum):
    TRANSIENT = "transient"          # timeout / 5xx / rate-limit / conn reset -> safe to retry
    NOT_FOUND = "not_found"          # 404 / no such / missing -> try an alternative
    INVALID_INPUT = "invalid_input"  # bad args / validation / 400 -> fix the input
    PERMISSION = "permission"        # denied / 401 / 403 -> ask the owner / stop
    UNKNOWN = "unknown"              # unclassified -> report, don't guess


class Recovery(str, Enum):
    RETRY = "retry"
    FIX_INPUT = "fix_input"
    ALTERNATIVE = "alternative"
    ASK_OWNER = "ask_owner"
    GIVE_UP = "give_up"


# Order matters: the most specific / most-safety-relevant patterns win first.
_PATTERNS: tuple[tuple[FailureKind, "re.Pattern[str]"], ...] = (
    (FailureKind.PERMISSION, re.compile(
        r"\b(denied|unauthori[sz]ed|forbidden|permission|not allowed|401|403)\b", re.I)),
    (FailureKind.TRANSIENT, re.compile(
        r"\b(timed? ?out|timeout|temporar\w*|try again|rate.?limit|throttl\w*|"
        r"429|50[0-4]|unavailable|connection (?:reset|refused)|econnreset)\b", re.I)),
    (FailureKind.NOT_FOUND, re.compile(
        r"\b(not found|no such|404|does not exist|missing)\b", re.I)),
    (FailureKind.INVALID_INPUT, re.compile(
        r"\b(invalid|malformed|bad request|validation|unprocessable|400|"
        r"required (?:field|argument|parameter)|expected .+ got)\b", re.I)),
)

_RECOVERY: dict[FailureKind, Recovery] = {
    FailureKind.TRANSIENT: Recovery.RETRY,
    FailureKind.NOT_FOUND: Recovery.ALTERNATIVE,
    FailureKind.INVALID_INPUT: Recovery.FIX_INPUT,
    FailureKind.PERMISSION: Recovery.ASK_OWNER,
    FailureKind.UNKNOWN: Recovery.GIVE_UP,
}

_EMPTY_EVIDENCE = {"", "(no output)", "none", "null", "n/a"}


def classify_failure(text: str) -> FailureKind:
    """Classify a failure summary into a typed kind. Pure; never raises."""
    t = text or ""
    for kind, pattern in _PATTERNS:
        if pattern.search(t):
            return kind
    return FailureKind.UNKNOWN


def recovery_for(kind: FailureKind) -> Recovery:
    return _RECOVERY.get(kind, Recovery.GIVE_UP)


@dataclass(frozen=True)
class Verification:
    verified: bool
    note: str = ""


def verify_step(*, ok: bool, expected_evidence: str, summary: str) -> Verification:
    """Evidence-based verification of one step. A step is verified only if it succeeded AND —
    when it named expected evidence — actually produced some output to back that up. Never assume
    success. Pure; never raises."""
    if not ok:
        return Verification(False, "step did not succeed")
    if not expected_evidence:
        return Verification(True, "")  # nothing was claimed, nothing to check
    if (summary or "").strip().lower() in _EMPTY_EVIDENCE:
        return Verification(False, f"expected '{expected_evidence}' but the step produced no evidence")
    return Verification(True, "")
