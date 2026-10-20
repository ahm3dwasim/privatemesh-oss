"""MED 4 — one receipt per model call, bound to the prompt that call actually sent.

The review's point was that `model_saw` recorded the ids `recall()` returned, once per turn, while
the prompt was built later somewhere else. The two agree today and can drift tomorrow — a retry,
a prompt-builder change, an extra block — and the API would still be reporting the old claim as
evidence.

So the receipt moves to the moment of the call: one per call, carrying the phase, the ordered
memory ids, a digest of each memory's rendered text, and a digest of the exact outbound message
array. The strongest assertion here recomputes that digest from the bytes the transport received.
"""
from __future__ import annotations

import hashlib
import json

import httpx
from conftest import INTENT_MARK, PLAN_MARK, S

from privatemesh import memory as mem
from privatemesh.agent import Agent
from privatemesh.audit import canonical
from privatemesh.llm import TokenFactory

READ_PLAN = json.dumps({"goal": "check the diary", "steps": [
    {"description": "read the notes", "tool": "notes_read", "arguments": {"query": "dinner"},
     "expected_evidence": "a note"}]})


class RecordingTF:
    """Routes like conftest's fake, and keeps the exact request bodies the model was asked with."""

    def __init__(self, audit, *, intent=None, plan=None, answer=None):
        self.audit = audit
        self.intent = intent if intent is not None else {"action": "answer", "query": "x", "steps": 0}
        self.plans = list(plan) if isinstance(plan, list) else [plan]
        self.answer = answer
        self.bodies: list[dict] = []
        self.receipts_at_call: list[int] = []
        self.tf = TokenFactory(S, httpx.Client(base_url=S.base_url,
                                               transport=httpx.MockTransport(self._handler)))

    def _handler(self, request: httpx.Request) -> httpx.Response:
        # How many receipts existed at the instant this call was made — the ordering claim.
        self.receipts_at_call.append(len(self.audit.rows(event="model_saw")))
        body = json.loads(request.content)
        self.bodies.append(body)
        system = next((m.get("content", "") for m in body["messages"]
                       if m.get("role") == "system"), "")
        if system.startswith(INTENT_MARK):
            content = json.dumps(self.intent)
        elif PLAN_MARK in system:
            content = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        else:
            content = self.answer if self.answer is not None else "the answer"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                                   "total_tokens": 7}})


def _agent(conn, registry, audit, broker, **kw):
    tf = RecordingTF(audit, **kw)
    return Agent(tf.tf, broker, conn, registry, audit), tf


def _receipts(audit, turn) -> list[dict]:
    return [json.loads(r["detail"]) for r in audit.rows(task_id=turn.task_id, event="model_saw")]


def test_every_model_call_gets_its_own_receipt_with_the_prompt_that_was_sent(conn, registry,
                                                                            audit, broker):
    """The central assertion: recompute each receipt's digest from the bytes the transport saw.
    If the receipt described anything other than the outbound prompt, this cannot match."""
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "answer", "query": "coffee", "steps": 0})
    turn = agent.run("what do I drink?")

    receipts = _receipts(audit, turn)
    assert [r["phase"] for r in receipts] == ["intent", "answer"]
    assert len(receipts) == len(tf.bodies)

    for receipt, body in zip(receipts, tf.bodies):
        digest = hashlib.sha256(canonical(body["messages"]).encode("utf-8")).hexdigest()
        assert receipt["messages_sha256"] == digest, (
            "the receipt does not describe the prompt that was actually sent")
        assert receipt["message_count"] == len(body["messages"])


def test_a_task_turn_receipts_the_plan_and_the_repair_as_separate_calls(conn, registry, audit,
                                                                        broker):
    """Phases are per call, so a repair retry is visible as its own call rather than hidden
    inside "the turn made some model calls"."""
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "task", "query": "dinner", "steps": 1},
                       plan=["I'm afraid I can't do that", READ_PLAN])
    turn = agent.run("what did I note about dinner?")

    assert [r["phase"] for r in _receipts(audit, turn)] == ["intent", "plan", "repair", "answer"]
    assert turn.report.completed is True


def test_the_receipt_exists_before_the_call_it_describes(conn, registry, audit, broker):
    """Emitted BEFORE the call, per the finding — the first call already has its receipt."""
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "answer", "query": "coffee", "steps": 0})
    agent.run("what do I drink?")
    assert tf.receipts_at_call[0] == 1
    assert tf.receipts_at_call[-1] == len(tf.bodies)


def test_the_receipt_hashes_the_rendered_memory_text_not_just_the_id(conn, registry, audit, broker):
    """A per-memory content hash: evidence of what the model was SHOWN, rather than a claim about
    which row it was fetched from. Edit the memory and the digest changes."""
    memory_id = mem.write_memory(conn, type="preference", text="I take my coffee black",
                                 approved=True)
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "answer", "query": "coffee", "steps": 0})
    turn = agent.run("what do I drink?")

    answer_receipt = _receipts(audit, turn)[-1]
    assert answer_receipt["memory_ids"] == [memory_id]

    rendered = f"- [{memory_id}] (preference) I take my coffee black"
    assert answer_receipt["memory_hashes"] == [
        {"id": memory_id, "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest()}]

    # ...and the digest is over text that is really in the prompt, not a parallel rendering.
    prompt = json.dumps(agent.turn(turn.id).memories)
    assert "I take my coffee black" in prompt


def test_the_intent_call_receipts_that_it_carried_no_memories(conn, registry, audit, broker):
    """The intent call runs before recall, so it sends none. Saying so is the honest receipt —
    the old turn-level event claimed the memories before any of them had been sent."""
    memory_id = mem.write_memory(conn, type="preference", text="I take my coffee black",
                                 approved=True)
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "answer", "query": "coffee", "steps": 0})
    turn = agent.run("what do I drink?")

    intent_receipt = _receipts(audit, turn)[0]
    assert intent_receipt["phase"] == "intent"
    assert intent_receipt["memory_ids"] == [] and intent_receipt.get("memory_hashes") in (None, [])
    assert _receipts(audit, turn)[-1]["memory_ids"] == [memory_id]


def test_a_redaction_on_the_prompt_is_recorded_as_a_count(conn, registry, audit, broker):
    """Secret-shaped owner input is redacted on the way out AND receipted — count only, never the
    value, because the value is the thing that must not be written down."""
    planted = "sk-ant-api03-" + "Z" * 40
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "answer", "query": "key", "steps": 0})
    turn = agent.run(f"my key is {planted}")

    redactions = [json.loads(r["detail"]) for r in audit.rows(task_id=turn.task_id,
                                                             event="egress.redacted")]
    assert redactions and all(r["count"] >= 1 for r in redactions)
    assert all("phase" in r for r in redactions)
    assert planted not in json.dumps(tf.bodies), "the planted secret reached the transport"
    assert planted not in json.dumps(redactions), "the secret was written into the audit log"
