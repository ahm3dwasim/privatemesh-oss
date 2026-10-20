"""The broker: the one door, and the approval that opens it exactly once."""
from __future__ import annotations

import pytest

from privatemesh.approval import ApprovalError, args_fingerprint
from privatemesh.audit import AuditError


def _outbox(conn):
    return conn.execute("SELECT to_addr, body, transport FROM outbox ORDER BY id").fetchall()


def test_unknown_skill_is_denied_and_audited(broker, audit, conn):
    inv = broker.invoke("t1", "definitely_not_a_skill", {"x": 1})
    assert inv.status == "denied" and inv.decision == "deny"
    assert inv.ran is False
    events = [r["event"] for r in audit.rows(task_id="t1")]
    assert "skill.unknown" in events
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_read_tier_skill_runs_immediately_and_is_audited(broker, audit):
    inv = broker.invoke("t2", "notes_read", {})
    assert inv.status == "executed" and inv.ok is True
    records = [r for r in audit.rows(task_id="t2") if r["event"] == "policy.decision"]
    assert len(records) == 1
    assert '"executed":true' in records[0]["detail"]
    assert '"decision":"allow"' in records[0]["detail"]


def test_act_skill_needs_approval_then_executes_and_is_audited(broker, audit, conn):
    inv = broker.invoke("t3", "send_message", {"to": "alex", "body": "on my way"})
    assert inv.status == "needs_approval" and inv.approval_id
    assert inv.ran is False
    assert _outbox(conn) == []                       # nothing happened yet

    done = broker.decide(inv.approval_id, approve=True)
    assert done.status == "executed" and done.ok is True
    assert _outbox(conn)[0]["transport"] == "fake-local-outbox"

    events = [r["event"] for r in audit.rows(task_id="t3")]
    assert "approval.requested" in events and "approval.granted" in events
    granted = [r for r in audit.rows(task_id="t3", event="policy.decision") if '"approved":true' in r["detail"]]
    assert len(granted) == 1 and '"executed":true' in granted[0]["detail"]


def test_deny_stops_the_action_and_is_audited(broker, audit, conn):
    inv = broker.invoke("t4", "send_message", {"to": "alex", "body": "hello"})
    out = broker.decide(inv.approval_id, approve=False)
    assert out.status == "denied" and out.ran is False
    assert _outbox(conn) == []
    events = [r["event"] for r in audit.rows(task_id="t4")]
    assert "approval.denied" in events
    denied = [r for r in audit.rows(task_id="t4", event="policy.decision") if '"approved":false' in r["detail"]]
    assert len(denied) == 1


def test_an_approval_cannot_be_replayed_a_second_time(broker, conn):
    """The key demo claim: an approval covers ONE action, not a class of actions."""
    inv = broker.invoke("t5", "send_message", {"to": "alex", "body": "first"})
    broker.decide(inv.approval_id, approve=True)
    assert len(_outbox(conn)) == 1

    with pytest.raises(ApprovalError, match="already (used|consumed)"):
        broker.decide(inv.approval_id, approve=True)
    assert len(_outbox(conn)) == 1                    # the second attempt sent nothing


def test_an_approval_cannot_be_replayed_for_different_arguments(broker, approval_store, conn):
    req = approval_store.request("t6", "send_message", {"to": "alex", "body": "the agreed text"})
    approval_store.approve(req.id)
    with pytest.raises(ApprovalError, match="does not cover these arguments"):
        approval_store.consume(req.id, task_id="t6", tool="send_message",
                               arguments={"to": "alex", "body": "a DIFFERENT text"})
    assert _outbox(conn) == []

    with pytest.raises(ApprovalError, match="covers skill"):
        approval_store.consume(req.id, task_id="t6", tool="notes_write",
                               arguments={"to": "alex", "body": "the agreed text"})
    with pytest.raises(ApprovalError, match="was requested for task"):
        approval_store.consume(req.id, task_id="t-other", tool="send_message",
                               arguments={"to": "alex", "body": "the agreed text"})

    # ...and it still works for the action it was actually raised for.
    approval_store.consume(req.id, task_id="t6", tool="send_message",
                           arguments={"to": "alex", "body": "the agreed text"})


def test_a_second_identical_call_needs_its_own_approval(broker, conn):
    first = broker.invoke("t7", "send_message", {"to": "alex", "body": "same"})
    broker.decide(first.approval_id, approve=True)
    second = broker.invoke("t7", "send_message", {"to": "alex", "body": "same"})
    assert second.status == "needs_approval"
    assert second.approval_id != first.approval_id
    assert len(_outbox(conn)) == 1


def test_arguments_fingerprint_is_order_insensitive_but_value_sensitive():
    a = args_fingerprint("s", {"to": "x", "body": "y"})
    assert a == args_fingerprint("s", {"body": "y", "to": "x"})
    assert a != args_fingerprint("s", {"to": "x", "body": "z"})
    assert a != args_fingerprint("s", {"to": "x", "body": "y", "extra": 1})


def test_an_unattended_broker_denies_act_instead_of_waiting(registry, audit, conn):
    from privatemesh.broker import Broker

    unattended = Broker(registry, audit, interactive=False)
    inv = unattended.invoke("t8", "send_message", {"to": "alex", "body": "hi"})
    assert inv.status == "denied" and inv.approval_id is None
    assert _outbox(conn) == []


def test_a_grant_changes_what_runs(broker, grants):
    assert broker.invoke("t9", "notes_write", {"text": "buy milk"}).status == "executed"
    grants.set("notes_write", "deny")
    assert broker.invoke("t9b", "notes_write", {"text": "buy eggs"}).status == "denied"


def test_revoking_a_skill_between_request_and_approval_stops_it(broker, grants, audit, conn):
    """'Revoking a skill takes effect immediately' — checked at the moment the owner says yes,
    so an approval raised before the revocation cannot be used to outrun it."""
    inv = broker.invoke("t9c", "send_message", {"to": "alex", "body": "hello"})
    assert inv.status == "needs_approval"
    grants.set("send_message", "deny")

    refused = broker.decide(inv.approval_id, approve=True)
    assert refused.status == "denied" and refused.ran is False
    assert _outbox(conn) == []
    assert "approval.revoked" in [r["event"] for r in audit.rows(task_id="t9c")]


def test_a_failed_audit_write_is_raised_not_swallowed(broker, audit, monkeypatch):
    """An action that cannot be receipted is not reported as done."""
    def boom(*a, **kw):
        raise AuditError("disk on fire")

    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(AuditError, match="disk on fire"):
        broker.invoke("t10", "notes_read", {})


def test_audit_failure_before_execution_stops_the_action(registry, audit, conn, monkeypatch):
    from privatemesh.broker import Broker

    b = Broker(registry, audit)
    monkeypatch.setattr(audit, "record", lambda *a, **kw: (_ for _ in ()).throw(AuditError("nope")))
    with pytest.raises(AuditError):
        b.invoke("t11", "send_message", {"to": "a", "body": "b"})
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_model_saw_records_the_ids_that_left_the_machine(broker, audit):
    broker.record_model_saw("t12", [3, 1, 2], note="recall")
    rows = audit.rows(task_id="t12", event="model_saw")
    assert len(rows) == 1 and '"memory_ids":[3,1,2]' in rows[0]["detail"]


def test_a_skill_that_raises_becomes_a_failed_result_not_a_crash(audit):
    """A misbehaving skill is contained: the broker reports a failed action, and it is receipted."""
    from privatemesh.broker import Broker
    from privatemesh.skills import Risk, SkillRegistry, Tool, guarded

    def explode(args):
        raise RuntimeError("boom")

    reg = SkillRegistry([Tool(name="kaboom", description="d", parameters={}, risk=Risk.LOW,
                              fn=guarded("kaboom", explode), gate_name="Read")])
    inv = Broker(reg, audit).invoke("t13", "kaboom", {})
    assert inv.status == "executed" and inv.ok is False and "boom" in (inv.error or "")
    assert [r for r in audit.rows(task_id="t13", event="policy.decision")
            if '"ok":false' in r["detail"]]


def test_a_skill_returning_the_wrong_type_is_contained(audit):
    from privatemesh.broker import Broker
    from privatemesh.skills import Risk, SkillRegistry, Tool, guarded

    reg = SkillRegistry([Tool(name="sloppy", description="d", parameters={}, risk=Risk.LOW,
                              fn=guarded("sloppy", lambda a: "not a ToolResult"), gate_name="Read")])
    inv = Broker(reg, audit).invoke("t14", "sloppy", {})
    assert inv.ok is False and "ToolResult" in (inv.error or "")


def test_consume_itself_refuses_a_second_spend(approval_store, conn):
    """The single-use guarantee lives in `consume`, not in `decide`: spend the same approval twice
    directly and the second spend must fail. (The decide-level replay test above never reaches
    consume a second time, so it cannot protect this line on its own.)"""
    args = {"to": "alex", "body": "the agreed text"}
    req = approval_store.request("t7", "send_message", args)
    approval_store.approve(req.id)
    approval_store.consume(req.id, task_id="t7", tool="send_message", arguments=args)
    with pytest.raises(ApprovalError, match="already used"):
        approval_store.consume(req.id, task_id="t7", tool="send_message", arguments=args)
