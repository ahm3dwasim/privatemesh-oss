"""HIGH 1 — the receipt must exist before the action does, and the two failure modes must differ.

The review's probe was concrete: `send_message` inserted its outbox row inside `_execute()` and
only then called `_record()`, so a failing audit write left a real effect with no receipt for it.
These tests pin the corrected ordering: reserve first (and refuse to act if that fails), complete
after, and never report "it ran but the proof didn't" as an ordinary failure.
"""
from __future__ import annotations

import pytest

from privatemesh.audit import AuditError
from privatemesh.broker import Broker, UnevidencedExecution
from privatemesh.skills import Risk, SkillRegistry, Tool, ToolResult, guarded

Rows = lambda conn, table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _spy_registry(audit) -> SkillRegistry:
    """A skill that reports what the audit log looked like at the moment it was running."""
    seen: dict = {}

    def spy(arguments):
        seen["intents"] = len(audit.rows(event="policy.intent"))
        seen["decisions"] = len(audit.rows(event="policy.decision"))
        return ToolResult(ok=True, content="ran")

    tool = Tool(name="spy", description="d", parameters={}, risk=Risk.LOW,
                fn=guarded("spy", spy), gate_name="Read")
    registry = SkillRegistry([tool])
    registry.seen = seen          # type: ignore[attr-defined]
    return registry


def test_the_intent_receipt_is_written_after_the_skill_ran_but_before_it_was_run(audit):
    """The ordering assertion, taken from inside the skill itself.

    At the instant the executor body runs, the reservation must already be on the chain and the
    completion must not be — that is what "receipt before action" means in an append-only log."""
    registry = _spy_registry(audit)
    inv = Broker(registry, audit).invoke("t1", "spy", {})

    assert inv.status == "executed" and inv.ok is True
    assert registry.seen["intents"] == 1        # reserved before the body executed
    assert registry.seen["decisions"] == 0      # and not yet completed while it was running
    intents = audit.rows(event="policy.intent")
    assert '"executed":false' in intents[0]["detail"]
    # The completion names the reservation it completes, so the pair reads as one record.
    done = audit.rows(event="policy.decision")[0]
    assert f"[intent #{intents[0]['id']}]" in done["detail"]


def test_the_reservation_is_written_before_an_approval_is_raised(broker, audit):
    """Raising an approval is a durable state change too (it becomes spendable), so it gets the
    same ordering. Nothing about it should be able to happen unreceipted."""
    inv = broker.invoke("t2", "send_message", {"to": "alex", "body": "hi"})
    assert inv.status == "needs_approval"
    events = [r["event"] for r in audit.rows(task_id="t2")]
    assert events.index("policy.intent") < events.index("approval.requested")


def test_an_unwritable_reservation_stops_the_skill_from_running(registry, audit, conn, monkeypatch):
    """The finding's exact scenario. Pre-fix this wrote a note and THEN failed the receipt, so the
    note existed with nothing recording it. Now the reservation is refused first, so the skill
    never runs and the store is untouched."""
    def boom(*a, **kw):
        raise AuditError("disk on fire")

    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(AuditError):
        Broker(registry, audit).invoke("t3", "notes_write", {"text": "must not be written"})
    assert Rows(conn, "notes") == 0


def test_a_skill_that_ran_without_its_completion_receipt_says_exactly_that(registry, audit, conn):
    """The other half of the distinction, and the one a product selling receipts must not blur.

    Let the reservation succeed and the completion fail, and the skill really does run. That is
    NOT an ordinary audit error — something happened and the proof of it did not — so it raises a
    distinct, loud error that names the reservation it belongs to."""
    real = audit.record

    def fail_completion(event, **kw):
        if event == "policy.decision":
            raise AuditError("the receipt store died mid-turn")
        return real(event, **kw)

    audit.record = fail_completion          # instance attribute shadows the bound method
    with pytest.raises(UnevidencedExecution) as caught:
        Broker(registry, audit).invoke("t4", "notes_write", {"text": "this one really ran"})

    message = str(caught.value)
    assert "RAN" in message and "not recorded as done" in message
    assert Rows(conn, "notes") == 1                       # it did happen
    assert audit.rows(event="policy.intent")              # and the reservation proves it
    assert caught.value.intent_id == audit.rows(event="policy.intent")[0]["id"]


def test_the_distinction_survives_to_the_caller_as_a_different_exception_type(registry, audit):
    """`UnevidencedExecution` is an `AuditError`, so existing failure handling still catches it —
    but a caller that wants to tell the two apart can, without parsing a message."""
    assert issubclass(UnevidencedExecution, AuditError)
