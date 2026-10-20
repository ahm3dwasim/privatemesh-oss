# Ported from the author's prior private assistant (Jarvis); adapted for PrivateMesh.
"""The skill contract — what every skill declares, and the registry that holds them.

Ported from the prior assistant's `tools/types.py` (the `Risk` ladder, `ToolResult`, and the
frozen `Tool` dataclass with its gate-name mapping onto the permission tiers), with that
project's provider-specific `ToolSpec` / credential-broker plumbing dropped.

A `Tool` is pure metadata plus a callable: name, description, JSON-Schema parameters, a **risk
level**, a **gate name** that maps it onto the permission tiers (so policy stays the single
source of truth), a **scope** string the owner can read, and the executor. The model only ever
*requests* a skill by name and arguments; the broker is what runs `fn`, and only after policy
has decided.

**The broker is the only path.** Every executor is registered wrapped by `guarded()`, which
refuses to run unless the call arrived through `broker.invoke` / `broker.decide` — the wrapper
checks a private context token that only the broker sets. That is a structural claim, not a
convention: `tests/test_bypass.py` calls a registered executor directly and expects a
`PermissionError`, and a mutant that removes the guard makes the suite fail.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Iterator

# The broker's own token object. Identity-checked, never exported: `broker.py` imports this
# module's private `_broker_context` and nothing else can produce the same object.
_BROKER_TOKEN = object()
_ACTIVE: contextvars.ContextVar[object | None] = contextvars.ContextVar("privatemesh_broker_call",
                                                                      default=None)


class Risk(IntEnum):
    """Ordered risk levels. Higher = more autonomy withheld from the model."""

    LOW = 1        # read-only, stays on the machine (web research, a note read) -> auto
    MEDIUM = 2     # writes or local effects (note write, draft) -> tier/sensitivity decides
    HIGH = 3       # external effect (send a message) -> always needs approval
    VERY_HIGH = 4  # money / irreversible (purchase, transfer) -> always needs approval
    BLOCKED = 5    # never allowed (production delete) -> denied outright


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str = ""
    error: str | None = None
    meta: dict = field(default_factory=dict)


ToolFn = Callable[[dict], ToolResult]


def is_broker_call() -> bool:
    """True while a broker-gated skill call is on the stack. Exposed for tests and diagnostics."""
    return _ACTIVE.get() is _BROKER_TOKEN


@contextlib.contextmanager
def _broker_context() -> Iterator[None]:
    """Mark the current context as a broker-issued call. Private: the broker is the only caller."""
    token = _ACTIVE.set(_BROKER_TOKEN)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def guarded(name: str, fn: ToolFn) -> ToolFn:
    """Wrap a skill executor so it refuses to run outside the broker.

    A `PermissionError` here is the intended outcome of a bypass attempt — the skill does not
    execute, and nothing is written to the audit log, so the attempt is visibly unreceipted."""

    def wrapper(arguments: dict) -> ToolResult:
        if not is_broker_call():
            raise PermissionError(
                f"skill '{name}' must be invoked through the broker "
                f"(broker.invoke / broker.decide); direct execution is not permitted"
            )
        return fn(arguments)

    wrapper.__name__ = f"guarded_{name}"
    wrapper.__doc__ = f"Broker-gated executor for skill '{name}'."
    return wrapper


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict          # JSON Schema (object) advertised to the model
    risk: Risk
    fn: ToolFn
    gate_name: str            # maps to policy tiers/sensitivity (e.g. "Read", "Write", "SendMessage")
    scope: str = ""           # human-readable capability, e.g. "web.read"
    timeout: float = 30.0
    retries: int = 0          # retries allowed for SAFE (idempotent) skills only
    approval_required: bool = False   # force owner approval regardless of tier/risk

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Tool needs a name")
        if self.risk not in Risk:
            raise ValueError(f"Tool {self.name!r}: risk must be a Risk member")

    def to_spec(self) -> dict:
        """The shape handed to the model in a plan prompt (no executor, no risk leakage)."""
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters, "scope": self.scope}


class SkillRegistry:
    """The declared skills. Lookups are by exact name — there is no fuzzy resolution, so a model
    cannot get a near-miss skill by guessing."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or ():
            self.register(t)

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"skill '{tool.name}' is already registered")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"unknown skill '{name}'")
        return tool

    def names(self) -> set[str]:
        return set(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def is_retriable(self, name: str) -> bool:
        """A skill may be retried only if it is explicitly declared retriable AND read-only —
        a retry must never blindly re-fire a side effect."""
        tool = self._tools.get(name)
        return bool(tool and tool.retries > 0 and tool.risk <= Risk.LOW)
