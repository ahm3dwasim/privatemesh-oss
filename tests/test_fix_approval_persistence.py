"""MED 6 — approvals and paused turns must outlive the process.

The old `ApprovalStore` was a dict, documented as failing closed because a forgotten approval can
never be replayed. True, and beside the point: it also forgot the owner's PENDING decisions and
the mapping needed to resume the turn that was waiting on one. A restart between "approve" and
"execute" left the owner believing an action had been handled while it was silently abandoned.

These tests restart the stack for real — new connections to the same files, new stores, a new
agent — and check that nothing the owner decided is lost.
"""
from __future__ import annotations

import json
import threading

import pytest
from conftest import fake_llm

from privatemesh import memory as mem
from privatemesh.agent import Agent, ANSWERED, NEEDS_APPROVAL
from privatemesh.approval import APPROVED, ApprovalError, ApprovalStore
from privatemesh.audit import AuditLog
from privatemesh.broker import Broker
from privatemesh.grants import GrantStore
from privatemesh.skills_builtin import build_registry
from privatemesh.turnstore import TurnStore

SEND_PLAN = json.dumps({"goal": "tell alex", "steps": [
    {"description": "read the notes", "tool": "notes_read", "arguments": {},
     "expected_evidence": "notes"},
    {"description": "message alex", "tool": "send_message",
     "arguments": {"to": "alex", "body": "running late"}, "expected_evidence": "recorded"}]})


def _stack(tmp_path, *, intent=None, plan=None, conn=None):
    """One 'process': its own handles onto the shared files."""
    conn = conn if conn is not None else mem.connect(str(tmp_path / "pm.db"))
    mem.init_schema(conn)
    audit = AuditLog(str(tmp_path / "audit.db"))
    grants = GrantStore(conn)
    approvals = ApprovalStore(conn)
    turns = TurnStore(conn)
    registry = build_registry(conn=conn, tavily_key="")
    tf = fake_llm(intent=intent, plan=plan)
    broker = Broker(registry, audit, approvals=approvals, grants=grants)
    agent = Agent(tf.tf, broker, conn, registry, audit, turns=turns)
    return {"conn": conn, "audit": audit, "approvals": approvals, "turns": turns,
            "grants": grants, "registry": registry, "broker": broker, "agent": agent, "tf": tf}


def _outbox(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]


# ---- approvals --------------------------------------------------------------------------------
def test_a_pending_approval_survives_a_restart(tmp_path):
    first = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    req = first.request("t1", "send_message", {"to": "alex", "body": "hello"})

    restarted = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    recovered = restarted.get(req.id)

    assert recovered is not None, "a pending approval was forgotten by a restart"
    assert recovered.arguments == {"to": "alex", "body": "hello"}
    assert recovered.args_hash == req.args_hash
    assert [r.id for r in restarted.recoverable()["pending"]] == [req.id]


def test_an_approved_but_unspent_approval_is_recoverable_after_a_restart(tmp_path):
    """The case the finding named: the owner said yes, the process died before the skill ran."""
    first = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    req = first.request("t2", "send_message", {"to": "alex", "body": "hello"})
    first.approve(req.id)

    restarted = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    recoverable = restarted.recoverable()

    assert recoverable["pending"] == []
    assert [r.id for r in recoverable["approved_unconsumed"]] == [req.id]
    assert restarted.get(req.id).status == APPROVED
    # ...and it is still spendable exactly once by the restarted process.
    restarted.consume(req.id, task_id="t2", tool="send_message",
                      arguments={"to": "alex", "body": "hello"})
    with pytest.raises(ApprovalError, match="already used"):
        restarted.consume(req.id, task_id="t2", tool="send_message",
                          arguments={"to": "alex", "body": "hello"})


def test_a_consumed_approval_is_not_offered_for_recovery(tmp_path):
    store = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    req = store.request("t3", "send_message", {"to": "a", "body": "b"})
    store.approve(req.id)
    store.consume(req.id, task_id="t3", tool="send_message", arguments={"to": "a", "body": "b"})

    restarted = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    assert restarted.recoverable()["approved_unconsumed"] == []
    assert [r.id for r in restarted.list(status="consumed")] == [req.id]


def test_only_one_racer_can_spend_an_approval(tmp_path):
    """`consume` is a compare-and-set, so the spend is decided by the database rather than by a
    read that happened to still be true when the write landed."""
    store = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    req = store.request("t4", "send_message", {"to": "alex", "body": "once"})
    store.approve(req.id)

    args = {"to": "alex", "body": "once"}
    wins, losses = [], []

    def spend() -> None:
        try:
            store.consume(req.id, task_id="t4", tool="send_message", arguments=args)
            wins.append(1)
        except ApprovalError:
            losses.append(1)

    threads = [threading.Thread(target=spend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wins) == 1, f"{len(wins)} racers spent one approval"
    assert len(losses) == 7


def test_a_denied_approval_stays_denied_across_a_restart(tmp_path):
    store = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    req = store.request("t5", "send_message", {"to": "a", "body": "b"})
    store.deny(req.id)

    restarted = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    with pytest.raises(ApprovalError, match="already denied"):
        restarted.approve(req.id)
    assert restarted.recoverable()["approved_unconsumed"] == []


def test_the_transition_timestamps_survive(tmp_path):
    store = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    req = store.request("t6", "send_message", {"to": "a", "body": "b"})
    store.approve(req.id)
    store.consume(req.id, task_id="t6", tool="send_message", arguments={"to": "a", "body": "b"})

    restarted = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    recovered = restarted.get(req.id)
    assert recovered.requested_at and recovered.decided_at and recovered.consumed_at


# ---- the paused turn --------------------------------------------------------------------------
def test_a_paused_turn_is_restored_and_resumed_by_a_new_process(tmp_path):
    """The end-to-end claim: a run that paused for approval completes after a restart, once the
    owner's decision is spent — and the skill still goes through the broker on the way."""
    first = _stack(tmp_path, intent={"action": "task", "query": "alex", "steps": 2},
                   plan=SEND_PLAN)
    turn = first["agent"].run("tell alex I'm running late")
    assert turn.status == NEEDS_APPROVAL and turn.approval_id
    assert _outbox(first["conn"]) == 0

    # ---- restart: brand-new connections, stores, registry and agent over the same files -------
    second = _stack(tmp_path, intent={"action": "task", "query": "alex", "steps": 2},
                    plan=SEND_PLAN)

    # This process has never seen the turn, so the recovery has to go through the database: the
    # approval names the turn it paused, and that link is what finds it.
    assert second["agent"]._turns == {}
    second["approvals"].approve(turn.approval_id)         # the owner's answer, in the new process
    assert [r.id for r in second["approvals"].recoverable()["approved_unconsumed"]] \
        == [turn.approval_id]

    resumed = second["agent"].resume_for_approval(turn.approval_id)
    assert resumed is not None, "the decision did not resume the run it was made for"
    assert resumed.status == ANSWERED and resumed.report.completed is True
    assert _outbox(second["conn"]) == 1


def test_the_restored_turn_resumes_at_the_step_it_paused_on(tmp_path):
    """Not from the top: the steps already completed are carried across, so a restored run does
    not re-execute the work it already did."""
    first = _stack(tmp_path, intent={"action": "task", "query": "alex", "steps": 2},
                   plan=SEND_PLAN)
    turn = first["agent"].run("tell alex I'm running late")
    assert turn.paused_step == 1

    second = _stack(tmp_path, intent={"action": "task", "query": "alex", "steps": 2},
                    plan=SEND_PLAN)
    restored = second["agent"].turn(turn.id)
    assert restored is not None
    assert restored.paused_step == 1
    assert restored.approval_id == turn.approval_id
    assert restored.plan is not None and len(restored.plan.steps) == 2
    # Step 1 completed and is carried across; step 2 is present but unrun, holding the approval
    # it is waiting on — the restored run knows exactly where it stopped.
    assert [o.index for o in restored.report.outcomes] == [0, 1]
    assert restored.report.outcomes[0].ok is True
    assert restored.report.outcomes[1].approval_id == turn.approval_id

    second["approvals"].approve(turn.approval_id)
    resumed = second["agent"].resume_for_approval(turn.approval_id)
    assert resumed.status == ANSWERED
    assert _outbox(second["conn"]) == 1                       # sent once, not twice


def test_the_approval_names_the_turn_it_paused(tmp_path):
    """The other direction of the link, and the one a restart needs to find both halves."""
    stack = _stack(tmp_path, intent={"action": "task", "query": "alex", "steps": 2},
                   plan=SEND_PLAN)
    turn = stack["agent"].run("tell alex I'm running late")

    restarted = ApprovalStore(mem.connect(str(tmp_path / "pm.db")))
    assert restarted.turn_for(turn.approval_id) == turn.id
    assert restarted.recoverable()["pending"][0].turn_id == turn.id


def test_the_owner_can_ask_what_is_still_waiting_after_a_restart(tmp_path):
    """Recoverable is only useful if the owner can see it, so the API exposes it: what is
    pending, what was granted but never spent, and the turn each one would resume."""
    from fastapi.testclient import TestClient

    from privatemesh.api import build_app
    from conftest import S

    first = _stack(tmp_path, intent={"action": "task", "query": "alex", "steps": 2},
                   plan=SEND_PLAN)
    turn = first["agent"].run("tell alex I'm running late")

    second = _stack(tmp_path, intent={"action": "task", "query": "alex", "steps": 2},
                    plan=SEND_PLAN)
    client = TestClient(build_app(agent=second["agent"], conn=second["conn"],
                                  audit=second["audit"], broker=second["broker"],
                                  grants=second["grants"], llm=second["tf"].tf,
                                  registry=second["registry"], settings=S))

    body = client.get("/approvals/recoverable").json()
    assert [r["id"] for r in body["pending"]] == [turn.approval_id]
    assert body["pending"][0]["turn_id"] == turn.id
    assert body["turns"] == [{"turn_id": turn.id, "status": NEEDS_APPROVAL,
                              "paused_step": 1, "approval_id": turn.approval_id}]
    assert body["approved_unconsumed"] == []

    # ...and answering through the API resumes the run the restart had left behind.
    decided = client.post(f"/approvals/{turn.approval_id}", json={"decision": "approve"})
    assert decided.status_code == 200
    assert decided.json()["turn"]["status"] == ANSWERED
    assert _outbox(second["conn"]) == 1
    assert client.get("/approvals/recoverable").json() == {"pending": [],
                                                           "approved_unconsumed": [], "turns": []}


def test_an_agent_without_a_store_behaves_exactly_as_before(conn, registry, audit, broker):
    """Durability is opt-in. The in-memory default still forgets a restart, which is the old
    fail-closed behaviour and stays available."""
    tf = fake_llm(intent={"action": "task", "query": "alex", "steps": 2}, plan=SEND_PLAN)
    agent = Agent(tf.tf, broker, conn, registry, audit)
    turn = agent.run("tell alex I'm running late")
    assert turn.status == NEEDS_APPROVAL
    assert agent.turns is None
    assert agent.turn("nope") is None
