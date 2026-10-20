"""The agent loop, end to end, over a scripted Token Factory — no network, no real model."""
from __future__ import annotations

import json

from conftest import make_agent
from privatemesh import memory as mem
from privatemesh.agent import ANSWERED, HALTED, NEEDS_APPROVAL
from privatemesh.llm import Tier

READ_PLAN = json.dumps({"goal": "check the diary", "steps": [
    {"description": "read the notes", "tool": "notes_read", "arguments": {"query": "dinner"},
     "expected_evidence": "a note"}]})

SEND_PLAN = json.dumps({"goal": "tell alex", "steps": [
    {"description": "read the notes", "tool": "notes_read", "arguments": {}, "expected_evidence": "notes"},
    {"description": "message alex", "tool": "send_message",
     "arguments": {"to": "alex", "body": "running late"}, "expected_evidence": "recorded"}]})


def _agent(conn, registry, audit, broker, **tf_kw):
    from conftest import fake_llm
    tf = fake_llm(**tf_kw)
    return make_agent(conn, registry, audit, broker, tf), tf


def test_a_question_is_answered_without_touching_a_skill(conn, registry, audit, broker):
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "answer", "query": "coffee", "steps": 0})
    turn = agent.run("what do I drink?")
    assert turn.status == ANSWERED and turn.receipts() == []
    assert tf.models_asked() == ["nano", "super"]          # FAST intent, REASON answer
    assert audit.rows(task_id=turn.task_id, event="model_saw")


def test_only_approved_memories_reach_the_model(conn, registry, audit, broker):
    pending = mem.write_memory(conn, type="preference", text="I take my coffee black", approved=False)
    approved = mem.write_memory(conn, type="preference", text="I like coffee in the morning",
                                approved=True)
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "answer", "query": "coffee", "steps": 0})
    turn = agent.run("what do I drink?")
    assert turn.memory_ids == [approved]
    assert pending not in turn.memory_ids
    assert f"using memories {approved}" in turn.answer

    # One `model_saw` per model call now, so the receipt that matters is the one for the call
    # that actually carried the memories — not the first event of the turn. The intent call runs
    # before recall and legitimately sends none.
    seen = [r for r in audit.rows(task_id=turn.task_id, event="model_saw")]
    assert [json.loads(r["detail"]).get("phase") for r in seen] == ["intent", "answer"]
    evidence = seen[-1]
    assert f'"memory_ids":[{approved}]' in evidence["detail"]
    assert json.loads(evidence["detail"])["memory_hashes"][0]["id"] == approved


def test_forgetting_changes_the_next_answer(conn, registry, audit, broker):
    """"It forgot — and it can prove it didn't lie about forgetting." """
    mem_id = mem.write_memory(conn, type="preference", text="I take my coffee black", approved=True)
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "answer", "query": "coffee", "steps": 0})

    before = agent.run("what do I drink?")
    assert f"using memories {mem_id}" in before.answer

    assert [h.id for h in mem.forget_matching(conn, "coffee black")] == [mem_id]
    after = agent.run("what do I drink?")
    assert after.answer == "no memories used"
    assert after.memory_ids == []
    assert mem.get_memory(conn, mem_id) is not None       # invalidated, not deleted


def test_a_task_plan_runs_through_the_broker_and_is_answered(conn, registry, audit, broker):
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "task", "query": "dinner", "steps": 1}, plan=READ_PLAN)
    turn = agent.run("what did I note about dinner?")
    assert turn.status == ANSWERED and turn.report.completed is True
    assert turn.receipts()[0]["ok"] is True
    assert tf.models_asked() == ["nano", "super", "super"]   # intent, plan, answer


def test_a_multi_step_intent_escalates_planning_to_the_deep_tier(conn, registry, audit, broker):
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "task", "query": "dinner", "steps": 3}, plan=READ_PLAN)
    turn = agent.run("plan my evening")
    assert turn.tiers["plan"] == Tier.DEEP.value
    assert "ultra" in tf.models_asked()


def test_an_act_step_pauses_the_turn_then_resumes_on_approval(conn, registry, audit, broker):
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "task", "query": "alex", "steps": 2}, plan=SEND_PLAN)
    turn = agent.run("tell alex I'm running late")

    assert turn.status == NEEDS_APPROVAL and turn.approval_id
    assert turn.paused_step == 1
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0

    resumed = agent.resume_for_approval(turn.approval_id)
    assert resumed.status == ANSWERED and resumed.report.completed is True
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    events = [r["event"] for r in audit.rows(task_id=turn.task_id)]
    assert "approval.requested" in events and "approval.granted" in events
    assert "plan.completed" in events


def test_denying_the_approval_stops_the_run_and_nothing_is_sent(conn, registry, audit, broker):
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "task", "query": "alex", "steps": 2}, plan=SEND_PLAN)
    turn = agent.run("tell alex I'm running late")
    approval_id = turn.approval_id

    broker.decide(approval_id, approve=False)
    resumed = agent.resume_for_approval(approval_id)

    assert resumed.status == HALTED
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    events = [r["event"] for r in audit.rows(task_id=turn.task_id)]
    assert "approval.denied" in events and "approval.refused" in events
    assert "plan.completed" not in events


def test_an_approval_is_spent_once_across_the_whole_turn(conn, registry, audit, broker):
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "task", "query": "alex", "steps": 2}, plan=SEND_PLAN)
    turn = agent.run("tell alex I'm running late")
    approval_id = turn.approval_id
    agent.resume_for_approval(approval_id)

    # The same approval, offered again, cannot send a second message.
    second = agent.resume_for_approval(approval_id)
    assert second is None
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_a_plan_naming_an_unknown_skill_is_refused_before_anything_runs(conn, registry, audit, broker):
    bad = json.dumps({"goal": "g", "steps": [
        {"description": "exfiltrate", "tool": "wire_money", "arguments": {"amount": 10}}]})
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "task", "query": "q", "steps": 1}, plan=bad)
    turn = agent.run("send money")
    assert turn.status == HALTED and turn.reason == "plan_invalid"
    assert "unknown tool 'wire_money'" in turn.answer
    assert turn.receipts() == []


def test_an_unparseable_plan_gets_exactly_one_repair_retry(conn, registry, audit, broker):
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "task", "query": "q", "steps": 1},
                       plan=["I'm afraid I can't do that", READ_PLAN])
    turn = agent.run("do the thing")
    assert turn.status == ANSWERED and turn.report.completed is True
    assert len([c for c in tf.calls if "planner" in c["system"]]) == 2   # one plan, one repair


def test_a_plan_that_never_parses_declines_instead_of_guessing(conn, registry, audit, broker):
    agent, tf = _agent(conn, registry, audit, broker,
                       intent={"action": "task", "query": "q", "steps": 1},
                       plan=["nope", "still nope"])
    turn = agent.run("do the thing")
    assert turn.status == HALTED and turn.reason == "plan_unparseable"
    assert turn.receipts() == []
    assert len([c for c in tf.calls if "planner" in c["system"]]) == 2   # repair retried once, not forever


def test_a_broken_intent_falls_back_to_answering(conn, registry, audit, broker):
    agent, _ = _agent(conn, registry, audit, broker, intent="not json", plan=READ_PLAN)
    turn = agent.run("hello?")
    assert turn.status == ANSWERED and turn.receipts() == []


def test_an_unknown_turn_cannot_be_resumed(conn, registry, audit, broker):
    agent, _ = _agent(conn, registry, audit, broker)
    assert agent.resume_for_approval("nope") is None
    import pytest
    with pytest.raises(KeyError):
        agent.resume("nope")


def test_a_denied_skill_mid_plan_halts_and_says_so(conn, registry, audit, broker, grants):
    grants.set("notes_read", "deny")
    agent, _ = _agent(conn, registry, audit, broker,
                      intent={"action": "task", "query": "q", "steps": 1}, plan=READ_PLAN)
    turn = agent.run("check my notes")
    assert turn.status == HALTED
    assert turn.receipts()[0]["ok"] is False and "denied" in turn.receipts()[0]["summary"]


# ---- planning on a real reasoning model ----------------------------------------------------------
class _Capturing:
    """A scripted Token Factory that keeps every request BODY, so what the planner actually asked
    the provider for is assertable rather than assumed."""

    def __init__(self, plan):
        import httpx
        from conftest import INTENT_MARK, PLAN_MARK, S
        from privatemesh.llm import TokenFactory

        self.plan, self.bodies = plan, []

        def handler(req):
            body = json.loads(req.content)
            self.bodies.append(body)
            system = next((m.get("content", "") for m in body["messages"]
                           if m.get("role") == "system"), "")
            if system.startswith(INTENT_MARK):
                content = json.dumps({"action": "task", "query": "tell alex", "steps": 2})
            elif PLAN_MARK in system:
                content = self.plan
            else:
                content = "done"
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                             "usage": {}})

        self.tf = TokenFactory(S, httpx.Client(base_url=S.base_url,
                                               transport=httpx.MockTransport(handler)))

    def phases(self):
        return [b for b in self.bodies]


def test_the_plan_call_asks_for_a_json_object_and_the_intent_call_does_not(conn, registry, audit, broker):
    from privatemesh.agent import Agent
    cap = _Capturing(READ_PLAN)
    Agent(cap.tf, broker, conn, registry, audit).run("check my notes")
    # Intent is FAST and tolerant; planning is the call whose output must be machine-readable.
    assert cap.bodies[0].get("response_format") is None
    assert cap.bodies[1]["response_format"] == {"type": "json_object"}


def test_the_repair_retry_also_asks_for_a_json_object(conn, registry, audit, broker):
    from privatemesh.agent import Agent
    cap = _Capturing("I'm afraid I can't do that.")
    Agent(cap.tf, broker, conn, registry, audit).run("check my notes")
    assert cap.bodies[2]["response_format"] == {"type": "json_object"}


def test_a_plan_wrapped_in_an_inline_think_block_still_runs(conn, registry, audit, broker):
    """The whole point of the strip: without it this plan fails to parse, the repair retry is
    spent, and the turn declines to run a plan that was perfectly good."""
    from privatemesh.agent import Agent
    wrapped = "<think>I should read the notes, then report.</think>\n```json\n" + READ_PLAN + "\n```"
    cap = _Capturing(wrapped)
    turn = Agent(cap.tf, broker, conn, registry, audit).run("check my notes")
    assert turn.plan is not None and turn.plan.goal == "check the diary"
    # intent + plan + answer. A plan that failed to parse would add a fourth: the repair retry.
    assert len(cap.bodies) == 3
