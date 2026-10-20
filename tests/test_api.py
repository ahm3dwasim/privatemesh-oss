"""API smoke tests over FastAPI's TestClient — the real app, a fake model, no network."""
from __future__ import annotations

import json

import pytest
from conftest import S, fake_llm, make_agent
from fastapi.testclient import TestClient

from privatemesh.api import build_app
from privatemesh.audit import AuditLog
from privatemesh.broker import Broker

SEND_PLAN = json.dumps({"goal": "tell alex", "steps": [
    {"description": "message alex", "tool": "send_message",
     "arguments": {"to": "alex", "body": "running late"}, "expected_evidence": "recorded"}]})


@pytest.fixture
def client(conn, registry, audit, grants):
    broker = Broker(registry, audit, grants=grants)
    tf = fake_llm(intent={"action": "answer", "query": "coffee", "steps": 0})
    agent = make_agent(conn, registry, audit, broker, tf)
    app = build_app(agent=agent, conn=conn, audit=audit, broker=broker, grants=grants,
                    llm=tf.tf, registry=registry, settings=S)
    return TestClient(app)


@pytest.fixture
def send_client(conn, registry, audit, grants):
    """A client whose scripted model plans one ACT step."""
    broker = Broker(registry, audit, grants=grants)
    tf = fake_llm(intent={"action": "task", "query": "alex", "steps": 1}, plan=SEND_PLAN)
    agent = make_agent(conn, registry, audit, broker, tf)
    app = build_app(agent=agent, conn=conn, audit=audit, broker=broker, grants=grants,
                    llm=tf.tf, registry=registry, settings=S)
    return TestClient(app)


def test_health_lists_the_skills(client):
    r = client.get("/health")
    assert r.status_code == 200 and "send_message" in r.json()["skills"]


def test_chat_answers_a_question_and_exposes_the_receipt(client):
    r = client.post("/chat", json={"message": "what do I drink?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and body["turn_id"]
    assert body["tiers"] == {"intent": "fast", "answer": "reason"}

    saw = client.get(f"/turns/{body['turn_id']}/model_saw").json()
    assert saw["memory_ids"] == [] and saw["events"]


def test_memories_start_pending_and_recall_follows_consent(client):
    created = client.post("/memories", json={"text": "I take my coffee black", "type": "preference"})
    assert created.status_code == 200
    memory_id = created.json()["id"]
    assert created.json()["approved"] is False

    listed = client.get("/memories").json()
    assert listed["memories"][0]["id"] == memory_id and listed["counts"]["pending"] == 1

    assert client.post(f"/memories/{memory_id}/approve").json() == {"id": memory_id, "approved": True}
    assert client.get("/memories").json()["counts"]["pending"] == 0

    assert client.post(f"/memories/{memory_id}/forget").json()["forgotten"] is True
    assert client.post(f"/memories/{memory_id}/forget").status_code == 200
    assert client.post("/memories/9999/approve").status_code == 404


def test_an_empty_or_fully_redacted_memory_is_rejected(client):
    assert client.post("/memories", json={"text": "   "}).status_code in (400, 422)


def test_grants_round_trip_and_unknown_skills_are_refused(client):
    assert client.get("/grants").json()["grants"] == {}
    ok = client.post("/grants", json={"skill": "notes_read", "mode": "deny"})
    assert ok.json() == {"skill": "notes_read", "mode": "deny"}
    assert client.get("/grants").json()["grants"] == {"notes_read": "deny"}
    assert client.post("/grants", json={"skill": "nope", "mode": "deny"}).status_code == 404
    assert client.post("/grants", json={"skill": "notes_read", "mode": "whatever"}).status_code == 400


def test_the_block_and_ask_flow_over_http(send_client, conn):
    """The demo moment: blocked, asked, approved, executed, receipted, and not replayable."""
    turn = send_client.post("/chat", json={"message": "tell alex I'm running late"}).json()
    assert turn["status"] == "needs_approval" and turn["approval_id"]
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0

    pending = send_client.get("/approvals", params={"status": "pending"}).json()["approvals"]
    assert [p["id"] for p in pending] == [turn["approval_id"]]
    assert pending[0]["arguments"] == {"to": "alex", "body": "running late"}

    decided = send_client.post(f"/approvals/{turn['approval_id']}", json={"decision": "approve"})
    assert decided.status_code == 200
    assert decided.json()["turn"]["status"] == "answered"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1

    # Replaying the same approval is refused, and changes nothing.
    again = send_client.post(f"/approvals/{turn['approval_id']}", json={"decision": "approve"})
    assert again.status_code == 409
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_denying_over_http_stops_the_run(send_client, conn):
    turn = send_client.post("/chat", json={"message": "tell alex I'm running late"}).json()
    denied = send_client.post(f"/approvals/{turn['approval_id']}", json={"decision": "deny"})
    assert denied.status_code == 200
    assert denied.json()["turn"]["status"] == "halted"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0

    assert send_client.post("/approvals/nope", json={"decision": "approve"}).status_code == 409
    assert send_client.post(f"/approvals/{turn['approval_id']}",
                            json={"decision": "maybe"}).status_code == 400


def test_the_audit_endpoints_expose_and_verify_the_chain(send_client):
    send_client.post("/chat", json={"message": "tell alex I'm running late"})
    rows = send_client.get("/audit").json()["rows"]
    events = [r["event"] for r in rows]
    assert "model_saw" in events and "policy.decision" in events and "approval.requested" in events

    verified = send_client.get("/audit/verify").json()
    assert verified["ok"] is True and verified["broken_id"] is None

    filtered = send_client.get("/audit", params={"event": "model_saw"}).json()["rows"]
    assert all(r["event"] == "model_saw" for r in filtered)


def test_a_paused_turn_can_be_resumed_by_turn_id(client, send_client):
    turn = send_client.post("/chat", json={"message": "tell alex I'm running late"}).json()
    resumed = send_client.post("/chat", json={"message": "continue",
                                              "turn_id": turn["turn_id"]}).json()
    assert resumed["status"] == "needs_approval"       # still waiting; nothing new started
    assert send_client.post("/chat", json={"message": "x", "turn_id": "nope"}).status_code == 404
    assert send_client.get("/turns/nope").status_code == 404


def test_usage_reports_the_tiers_that_were_called(client):
    client.post("/chat", json={"message": "hello"})
    usage = client.get("/usage").json()
    assert usage["calls"] >= 2 and usage["models"]["fast"] == "nano"
    assert usage["prompt_tokens"] > 0 and "nano" in usage["by_model"]


def test_memory_written_through_the_api_is_recalled_only_after_approval(conn, registry, audit,
                                                                       grants):
    broker = Broker(registry, audit, grants=grants)
    tf = fake_llm(intent={"action": "answer", "query": "coffee", "steps": 0})
    agent = make_agent(conn, registry, audit, broker, tf)
    client = TestClient(build_app(agent=agent, conn=conn, audit=audit, broker=broker,
                                  grants=grants, llm=tf.tf, registry=registry, settings=S))

    memory_id = client.post("/memories", json={"text": "I take my coffee black",
                                               "type": "preference"}).json()["id"]
    assert client.post("/chat", json={"message": "what do I drink?"}).json()["memory_ids"] == []

    client.post(f"/memories/{memory_id}/approve")
    assert client.post("/chat", json={"message": "what do I drink?"}).json()["memory_ids"] == [memory_id]

    client.post(f"/memories/{memory_id}/forget")
    assert client.post("/chat", json={"message": "what do I drink?"}).json()["memory_ids"] == []


def test_an_audit_failure_is_reported_as_a_failed_turn_not_a_success(monkeypatch, conn, registry,
                                                                     grants):
    """The product's promise is the receipt, so a turn that cannot be receipted is reported as
    FAILED — never as a quiet success."""
    from privatemesh.audit import AuditError

    audit = AuditLog(":memory:")
    broker = Broker(registry, audit, grants=grants)
    tf = fake_llm()
    agent = make_agent(conn, registry, audit, broker, tf)
    client = TestClient(build_app(agent=agent, conn=conn, audit=audit, broker=broker,
                                  grants=grants, llm=tf.tf, registry=registry, settings=S))
    monkeypatch.setattr(audit, "record", lambda *a, **kw: (_ for _ in ()).throw(AuditError("down")))
    body = client.post("/chat", json={"message": "hi"}).json()
    assert body["status"] == "error"
    assert "AuditError" in body["reason"] and "nothing was run" in body["answer"]
