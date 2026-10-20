"""`privatemesh smoke` over MockTransports — every turn, the cap, and the audit verdict.

The smoke cannot be run here (there is no key and no network in this worktree), so the thing
being tested is that it will do the right thing when the manager runs it: three turns, the right
tiers, the approval gate actually engaging, a hard call ceiling, and an exit code that means
something. Both transports are mocked, so nothing here can reach the network by accident.
"""
from __future__ import annotations

import json

import httpx
import pytest

from privatemesh import memory as mem
from privatemesh import smoke
from privatemesh.config import Settings

INTENT_MARK = "You route a personal assistant"
PLAN_MARK = "PrivateMesh's planner"

S = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast="nano",
             model_reason="super", model_deep="ultra", tavily_key="tvly-test")

SEND_PLAN = {"goal": "tell dana", "steps": [
    {"description": "message dana", "tool": "send_message",
     "arguments": {"to": "dana@example.com", "body": "the smoke test passed"},
     "expected_evidence": "recorded in the outbox"}]}

RESEARCH_PLAN = {"goal": "research", "steps": [
    {"description": "search the web", "tool": "web_research",
     "arguments": {"query": "Nebius Token Factory"}, "expected_evidence": "a result"}]}

# A plan that answers the send request WITHOUT a sensitive action — the turn runs happily and
# proves nothing, which is exactly the case the smoke has to call out.
HARMLESS_PLAN = {"goal": "look it up", "steps": [
    {"description": "read the notes", "tool": "notes_read", "arguments": {"query": "dana"},
     "expected_evidence": "a note"}]}


class Scripted:
    """Both sides of the wire: the model, routed on the prompt it was actually sent, and Tavily."""

    def __init__(self, *, send_plan=None):
        self.bodies = []
        self.send_plan = SEND_PLAN if send_plan is None else send_plan

    # -- the model ---------------------------------------------------------------------------
    def llm(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.bodies.append(body)
        messages = body["messages"]
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
        if system.startswith(INTENT_MARK):
            content = json.dumps(self._intent(user))
        elif PLAN_MARK in system:
            content = json.dumps(self._plan(system))
        else:
            content = "Here is the answer, in one line."
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}})

    @staticmethod
    def _intent(user: str) -> dict:
        if "dana@example.com" in user:
            return {"action": "task", "query": "message dana", "steps": 2}   # -> DEEP
        if "Nebius Token Factory" in user:
            return {"action": "task", "query": "Nebius Token Factory", "steps": 1}  # -> REASON
        return {"action": "answer", "query": user, "steps": 0}

    def _plan(self, system: str) -> dict:
        if "dana@example.com" in system:
            return self.send_plan
        return RESEARCH_PLAN

    # -- Tavily ------------------------------------------------------------------------------
    @staticmethod
    def tavily(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "answer": "Token Factory is Nebius's inference API.",
            "results": [{"title": "Token Factory", "url": "https://nebius.com/token-factory"}]})

    def plan_bodies(self) -> list[dict]:
        return [b for b in self.bodies
                if any(PLAN_MARK in m.get("content", "") for m in b["messages"])]

    def intent_bodies(self) -> list[dict]:
        return [b for b in self.bodies
                if b["messages"][0].get("content", "").startswith(INTENT_MARK)]


@pytest.fixture
def scripted():
    return Scripted()


def _run(scripted, tmp_path, **kw):
    lines: list[str] = []
    code = smoke.run_smoke(
        S, transport=httpx.MockTransport(scripted.llm),
        tavily_http=httpx.Client(transport=httpx.MockTransport(scripted.tavily)),
        data_dir=tmp_path / "smoke", out=lines.append, **kw)
    return code, "\n".join(lines)


def test_the_three_turns_run_and_the_audit_still_verifies(scripted, tmp_path):
    code, out = _run(scripted, tmp_path)
    assert code == 0, out
    assert "turn 1/3" in out and "turn 2/3" in out and "turn 3/3" in out
    assert "SMOKE PASS" in out
    assert out.count("audit:   ok") == 3
    assert "final audit verify: ok" in out


def test_each_turn_reports_its_tiers_models_latency_and_tokens(scripted, tmp_path):
    _, out = _run(scripted, tmp_path)
    assert "tiers:   answer=reason(super)" in out          # turn 1: recall answered on REASON
    assert "plan=reason(super)" in out                     # turn 2: one step -> REASON
    assert "plan=deep(ultra)" in out                        # turn 3: two steps -> DEEP
    assert out.count("\n  latency:") == 3 and out.count("\n  tokens:") == 3
    assert "nano" in out and "super" in out and "ultra" in out


def test_the_configured_tier_models_and_thinking_defaults_are_printed(scripted, tmp_path):
    _, out = _run(scripted, tmp_path)
    assert "fast   nano  (max_tokens 256, enable_thinking False)" in out
    assert "deep   ultra  (max_tokens 8192, enable_thinking True)" in out


def test_fast_never_thinks_and_deep_does_in_the_live_bodies(scripted, tmp_path):
    """The measured trap: thinking left on turns a short answer into a null one. Every live call
    must state its intent in the body."""
    _run(scripted, tmp_path)
    assert all(b["chat_template_kwargs"] == {"enable_thinking": False}
               for b in scripted.intent_bodies())
    deep = [b for b in scripted.plan_bodies() if b["model"] == "ultra"]
    assert deep and all(b["chat_template_kwargs"] == {"enable_thinking": True} for b in deep)
    assert all(b["chat_template_kwargs"]["enable_thinking"] is False
               for b in scripted.plan_bodies() if b["model"] == "super")


def test_the_send_turn_blocks_and_only_runs_once_approved(scripted, tmp_path):
    code, out = _run(scripted, tmp_path)
    assert code == 0
    assert "blocked, awaiting approval:" in out
    assert "(paused, then approved and resumed)" in out
    # The action really ran, and only after the approval: the outbox holds it.
    conn = mem.connect(str(tmp_path / "smoke" / "privatemesh.db"))
    rows = conn.execute("SELECT to_addr, body FROM outbox").fetchall()
    assert [tuple(r) for r in rows] == [("dana@example.com", "the smoke test passed")]


def test_the_memory_turn_answers_from_the_seeded_approved_memory(scripted, tmp_path):
    _, out = _run(scripted, tmp_path)
    memory_turn = out.split("turn 2/3")[0]
    assert smoke.SEED_QUESTION in memory_turn
    assert "Here is the answer" in memory_turn


def test_a_send_turn_that_never_pauses_is_reported_as_a_miss(scripted, tmp_path):
    """A smoke test that quietly passes when the gate never engaged is worse than no smoke test:
    it certifies the one behaviour it did not exercise."""
    scripted.send_plan = HARMLESS_PLAN
    code, out = _run(scripted, tmp_path)
    assert code == 1
    assert "MISS: this turn never paused for approval" in out
    assert "SMOKE FAIL" in out


def test_the_call_cap_stops_the_run_instead_of_billing(scripted, tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "SMOKE_CALL_CAP", 2)
    code, out = _run(scripted, tmp_path)
    assert code == 1
    assert "STOPPED: the 2-call cap is spent; this turn did not start" in out
    assert "model calls used: " in out
    assert len(scripted.bodies) <= 2


def test_a_missing_key_refuses_before_anything_is_built(tmp_path):
    lines: list[str] = []
    code = smoke.run_smoke(Settings("", S.base_url, "", "", "", ""), data_dir=tmp_path / "x",
                           out=lines.append)
    assert code == 1 and "NEBIUS_API_KEY" in lines[0]


def test_the_smoke_owns_its_data_directory(scripted, tmp_path):
    """A smoke run must never write into the owner's real memory store."""
    _run(scripted, tmp_path)
    assert (tmp_path / "smoke" / "privatemesh.db").exists()
    assert (tmp_path / "smoke" / "audit.db").exists()


def test_re_running_does_not_pile_up_seeded_memories(scripted, tmp_path):
    _run(scripted, tmp_path)
    _run(scripted, tmp_path)
    conn = mem.connect(str(tmp_path / "smoke" / "privatemesh.db"))
    rows = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
    assert rows[0] == 1


def test_no_reasoning_text_reaches_the_scripted_audit_or_the_printed_answer(scripted, tmp_path):
    """A model that inlines its reasoning must not have that reasoning recorded as the answer."""
    def with_reasoning(req):
        r = scripted.llm(req)
        body = r.json()
        body["choices"][0]["message"]["reasoning_content"] = "SECRET SCRATCHPAD"
        return httpx.Response(200, json=body)

    lines: list[str] = []
    smoke.run_smoke(S, transport=httpx.MockTransport(with_reasoning),
                    tavily_http=httpx.Client(transport=httpx.MockTransport(scripted.tavily)),
                    data_dir=tmp_path / "smoke", out=lines.append)
    out = "\n".join(lines)
    assert "SECRET SCRATCHPAD" not in out


def test_a_tool_that_returned_an_error_fails_the_run(scripted, tmp_path):
    """`ok=False` on a step receipt means the wire is broken. Reporting PASS because the process
    survived would be the smoke test lying about the one thing it was run to check."""
    def refusal(req):
        if "tavily" in str(req.url) or req.url.host != "tf.test":
            return httpx.Response(200, json={})
        return scripted.tavily(req)

    lines: list[str] = []
    code = smoke.run_smoke(
        S, transport=httpx.MockTransport(scripted.llm),
        tavily_http=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(500, json={"error": "tavily down"}))),
        data_dir=tmp_path / "smoke", out=lines.append)
    out = "\n".join(lines)
    assert code == 1 and "FAILED STEP(S)" in out and "SMOKE FAIL" in out
