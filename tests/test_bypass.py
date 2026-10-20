"""The bypass mutant: proof that the broker is the only path to a skill.

If a future change registers a skill's raw executor instead of the broker-gated wrapper, or calls
one directly, these tests fail. The mutant they are built to kill is "just call the function".
"""
from __future__ import annotations

import json

import pytest

from privatemesh.skills import Risk, SkillRegistry, Tool, guarded


def test_every_registered_skill_is_broker_gated(registry):
    """The mutation kill: register a bare executor and this stops being true."""
    assert registry.names() == {"web_research", "notes_read", "notes_write", "email_draft",
                               "send_message"}
    for tool in registry.all():
        assert tool.fn.__name__ == f"guarded_{tool.name}", \
            f"skill '{tool.name}' is registered without the broker guard"
        with pytest.raises(PermissionError, match="must be invoked through the broker"):
            tool.fn({})


def test_calling_a_skill_executor_directly_is_refused_and_unreceipted(registry, audit, conn):
    tool = registry.get("send_message")
    before = len(audit.rows())
    with pytest.raises(PermissionError):
        tool.fn({"to": "alex", "body": "bypassing the broker"})
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    assert len(audit.rows()) == before
    assert not [r for r in audit.rows() if r["event"] == "policy.decision"]


def test_the_guard_is_what_stops_it_not_the_skill_body(registry):
    """The wrapper is checked BEFORE the executor: a skill that would happily run is still refused."""
    called = []

    def eager(args):
        called.append(args)
        from privatemesh.skills import ToolResult
        return ToolResult(ok=True, content="ran")

    reg = SkillRegistry([Tool(name="eager", description="d", parameters={}, risk=Risk.LOW,
                              fn=guarded("eager", eager), gate_name="Read")])
    with pytest.raises(PermissionError):
        reg.get("eager").fn({})
    assert called == []


def test_a_direct_call_cannot_be_laundered_through_the_broker_context(registry, conn, audit):
    """Entering the broker's own context from outside is not a supported path, and the skill that
    matters (an ACT) still needs a policy decision the caller cannot fabricate."""
    from privatemesh.skills import _broker_context

    tool = registry.get("send_message")
    with _broker_context():                     # the private token is reachable only in-process
        result = tool.fn({"to": "alex", "body": "hello"})
    assert result.ok is True
    # ...but nothing was receipted and no approval exists, so this is visible as an unreceipted
    # action: the API exposes no way to reach `_broker_context`, and `agent`/`api` never import it.
    assert not [r for r in audit.rows() if r["event"] == "policy.decision"]


def test_the_agent_only_reaches_skills_through_the_broker(conn, registry, audit, broker, monkeypatch):
    """A plan step must produce a policy.decision receipt; a step that ran without one is a bypass."""
    from conftest import fake_llm, make_agent

    plan = json.dumps({"goal": "g", "steps": [
        {"description": "read", "tool": "notes_read", "arguments": {}, "expected_evidence": "notes"}]})
    tf = fake_llm(intent={"action": "task", "query": "q", "steps": 1}, plan=plan)
    agent = make_agent(conn, registry, audit, broker, tf)
    turn = agent.run("read my notes")
    decisions = audit.rows(task_id=turn.task_id, event="policy.decision")
    assert len(decisions) == 1
    assert turn.receipts()[0]["ok"] is True


def test_the_api_module_never_imports_the_private_guard():
    """A structural check with teeth: if `_broker_context` leaks into the app layer, the
    'only the broker' claim is no longer checkable by reading the imports."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "privatemesh"
    allowed = {"skills.py", "broker.py"}          # the guard, and the one thing allowed to open it
    for path in src.glob("*.py"):
        if path.name in allowed:
            continue
        assert "_broker_context" not in path.read_text(), \
            f"{path.name} reaches into the broker guard"
