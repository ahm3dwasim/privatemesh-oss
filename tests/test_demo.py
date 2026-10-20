"""Demo mode, CORS, and the two view endpoints the web app is built on.

The demo is the judge-facing path, so it is pinned end to end here: the canned string the UI offers
must reach an approval card, the approval must stamp a receipt that carries a real chain hash, and
the chain must still verify afterwards. Everything runs over the production kernel with a scripted
model — no network, no keys.
"""
from __future__ import annotations


import pytest
from conftest import S
from fastapi.testclient import TestClient

from privatemesh import demo
from privatemesh.agent import Agent
from privatemesh.api import build_app
from privatemesh.audit import AuditLog
from privatemesh.broker import Broker
from privatemesh.grants import GrantStore


@pytest.fixture
def demo_app(conn, tmp_path, monkeypatch):
    """A demo-wired app over an in-memory store (no `data/` directory is touched)."""
    monkeypatch.chdir(tmp_path)
    audit = AuditLog(":memory:")
    settings = demo.demo_settings(S)
    registry = demo.demo_registry(conn, settings)
    seeded = demo.seed_memories(conn)
    llm = demo.demo_llm(settings)
    grants = GrantStore()
    broker = Broker(registry, audit, grants=grants)
    agent = Agent(llm, broker, conn, registry, audit)
    app = build_app(agent=agent, conn=conn, audit=audit, broker=broker, grants=grants,
                    llm=llm, registry=registry, settings=settings)
    return TestClient(app), conn, seeded


def _send_the_canned_message(client) -> dict:
    r = client.post("/chat", json={"message": demo.CANNED_TASK})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ the scripted router ---------
def test_canned_task_is_an_action_and_the_question_is_not():
    """The router is what makes the demo reproducible. Both halves are asserted, because a demo
    where everything asks for approval is as broken as one where nothing does."""
    assert demo.intent_for(demo.CANNED_TASK)["action"] == "task"
    assert demo.intent_for(demo.CANNED_QUESTION)["action"] == "answer"

    steps = demo.plan_for(demo.CANNED_TASK)["steps"]
    assert [s["tool"] for s in steps] == ["send_message"]
    assert steps[0]["arguments"]["to"] and steps[0]["arguments"]["body"]


def test_canned_question_is_answered_without_touching_anything(demo_app):
    client, _, _ = demo_app
    turn = client.post("/chat", json={"message": demo.CANNED_QUESTION}).json()
    assert turn["status"] == "answered", turn
    assert turn["approval_id"] is None
    assert turn["answer"].strip()


# ------------------------------------------------------------------ the block-and-ask moment ----
def test_canned_message_pauses_for_approval_then_prints_an_approved_receipt(demo_app):
    client, conn, _ = demo_app
    turn = _send_the_canned_message(client)
    assert turn["status"] == "needs_approval", turn
    assert turn["approval_id"]

    # The pending receipt: the arguments are readable BEFORE anything runs.
    pending = client.get(f"/turns/{turn['turn_id']}/receipts").json()["receipts"]
    assert len(pending) == 1
    assert pending[0]["tool"] == "send_message"
    assert pending[0]["verdict"] == "pending"
    assert pending[0]["decided_by"] == "waiting"
    assert pending[0]["approved"] is None
    assert pending[0]["arguments"]["to"] == "Alex (investor contact)"
    assert pending[0]["executed"] is False

    r = client.post(f"/approvals/{turn['approval_id']}", json={"decision": "approve"})
    assert r.status_code == 200, r.text
    assert r.json()["turn"]["status"] == "answered"

    # The printed receipt: approved, executed, and carrying a hash that is IN the chain.
    printed = client.get(f"/turns/{turn['turn_id']}/receipts").json()["receipts"]
    assert len(printed) == 1, "the pause and the resume are ONE receipt, not two"
    card = printed[0]
    assert card["verdict"] == "approved", card
    assert card["decided_by"] == "you"
    # The kernel's raw policy verdict is still `needs_approval` — the owner overrode it. A receipt
    # that reported "allow" here would be erasing who actually decided.
    assert card["decision"] == "needs_approval"
    assert card["approved"] is True
    assert card["executed"] is True and card["ok"] is True
    assert card["risk"] == "HIGH" and card["tier"] == "act"
    assert card["hash"] and card["ts"]
    assert card["hash"] in {row["hash"] for row in client.get("/audit").json()["rows"]}

    assert client.get("/audit/verify").json()["ok"] is True
    # The fake transport recorded it and nothing left the machine.
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_the_post_approval_answer_reports_what_actually_ran(demo_app):
    """The demo's copy must not contradict its own receipt. Before the decision nothing has run, so
    the turn says it is waiting; after it, the answer has to say that an action ran — not that the
    message was merely drafted. A demo whose narration disagrees with its audit is worse than one
    with no narration."""
    client, _, _ = demo_app
    turn = _send_the_canned_message(client)
    assert "waiting for your approval" in turn["answer"]

    client.post(f"/approvals/{turn['approval_id']}", json={"decision": "approve"})
    after = client.get(f"/turns/{turn['turn_id']}").json()
    assert "recorded for Alex" in after["answer"]
    assert "did not leave the machine" in after["answer"]


def test_a_replayed_approval_is_refused(demo_app):
    client, _, _ = demo_app
    turn = _send_the_canned_message(client)
    assert client.post(f"/approvals/{turn['approval_id']}",
                       json={"decision": "approve"}).status_code == 200
    again = client.post(f"/approvals/{turn['approval_id']}", json={"decision": "approve"})
    assert again.status_code == 409, "an approval is spendable exactly once"


def test_a_denied_action_is_receipted_as_refused_and_never_runs(demo_app):
    client, conn, _ = demo_app
    turn = _send_the_canned_message(client)
    r = client.post(f"/approvals/{turn['approval_id']}", json={"decision": "deny"})
    assert r.status_code == 200, r.text
    card = client.get(f"/turns/{turn['turn_id']}/receipts").json()["receipts"][0]
    assert card["verdict"] == "refused" and card["approved"] is False
    assert card["decided_by"] == "you"
    assert card["executed"] is False
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


# ------------------------------------------------------------------ memory: consent + proof -----
def test_the_seeded_pending_memory_is_not_recallable_until_approved(demo_app):
    client, conn, seeded = demo_app
    assert len(seeded) == 3
    listed = client.get("/memories").json()["memories"]
    pending = [m for m in listed if not m["approved"]]
    assert [m["text"] for m in pending] == ["The Series A target is $4M."]
    assert client.get("/memories", params={"approved_only": True}).json()["counts"]["pending"] == 1

    from privatemesh import memory as mem
    assert mem.recall(conn, "Series A target", k=6) == []

    client.post(f"/memories/{pending[0]['id']}/approve")
    assert [h.text for h in mem.recall(conn, "Series A target", k=6)] == \
        ["The Series A target is $4M."]


def test_a_forgotten_memory_stays_visible_with_its_invalid_at(demo_app):
    """"It can prove it forgot" is a screen, not a slogan: the row survives the forgetting."""
    client, _, _ = demo_app
    created = client.post("/memories", json={"text": "Temporary fact about the Berlin office.",
                                             "approved": True}).json()
    assert client.get("/memories/forgotten").json()["count"] == 0

    assert client.post(f"/memories/{created['id']}/forget").json()["forgotten"] is True

    gone = client.get("/memories/forgotten").json()
    assert gone["count"] == 1
    row = gone["memories"][0]
    assert row["id"] == created["id"] and row["text"] == "Temporary fact about the Berlin office."
    assert row["invalid_at"], "the panel strikes it through with a timestamp, so it must be here"
    assert created["id"] not in [m["id"] for m in client.get("/memories").json()["memories"]]


def test_seeding_twice_does_not_duplicate(demo_app):
    _, conn, _ = demo_app
    assert demo.seed_memories(conn) == []
    assert len(conn.execute("SELECT id FROM memories").fetchall()) == 3


# ------------------------------------------------------------------ the browser seam ------------
def test_cors_admits_the_web_origin_and_nobody_else(demo_app):
    client, _, _ = demo_app
    ok = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    stranger = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in stranger.headers


def test_pm_web_origins_replaces_the_default_list(monkeypatch, tmp_path):
    import dataclasses

    from privatemesh.api import build_app as _build
    from privatemesh.config import Settings

    monkeypatch.chdir(tmp_path)
    from privatemesh import memory as mem

    audit = AuditLog(":memory:")
    conn = mem.connect(":memory:")
    mem.init_schema(conn)
    settings = dataclasses.replace(S, web_origins="https://privatemesh.example")
    registry = demo.demo_registry(conn, settings)
    llm = demo.demo_llm(settings)
    broker = Broker(registry, audit, grants=GrantStore())
    agent = Agent(llm, broker, conn, registry, audit)
    app = _build(agent=agent, conn=conn, audit=audit, broker=broker, grants=GrantStore(), llm=llm,
                 registry=registry, settings=settings)
    client = TestClient(app)
    assert client.get("/health", headers={"Origin": "https://privatemesh.example"}
                      ).headers.get("access-control-allow-origin") == "https://privatemesh.example"
    assert "access-control-allow-origin" not in client.get(
        "/health", headers={"Origin": "http://localhost:3000"}).headers


# ------------------------------------------------------------------ usage + wiring --------------
def test_usage_counts_the_demo_calls_by_model(demo_app):
    client, _, _ = demo_app
    client.post("/chat", json={"message": demo.CANNED_QUESTION})
    u = client.get("/usage").json()
    assert u["calls"] >= 2 and u["prompt_tokens"] > 0
    assert set(u["by_model"]) <= {demo.DEMO_MODEL_FAST, demo.DEMO_MODEL_REASON, demo.DEMO_MODEL_DEEP}
    assert u["models"]["fast"] == demo.DEMO_MODEL_FAST


def test_default_app_in_demo_mode_keeps_its_own_data_dir(monkeypatch, tmp_path):
    """A demo run must never write into the owner's real store."""
    monkeypatch.chdir(tmp_path)
    from privatemesh.api import default_app

    app = default_app(demo=True)
    client = TestClient(app)
    assert client.get("/health").json()["ok"] is True
    assert len(client.get("/memories").json()["memories"]) == 3
    assert (tmp_path / "data" / "demo" / "privatemesh.db").exists()
    assert not (tmp_path / "data" / "privatemesh.db").exists()


def test_pm_demo_env_var_turns_it_on(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PM_DEMO", "1")
    from privatemesh.api import default_app

    assert TestClient(default_app()).get("/memories").json()["counts"]["total"] == 3


def test_cli_demo_defaults_to_the_web_apps_port(monkeypatch, tmp_path):
    """`serve --demo` with no explicit port must land on 8765 — the port the web app expects."""
    import uvicorn

    from privatemesh import cli

    monkeypatch.chdir(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, host=None, port=None, **kw: seen.update(host=host, port=port))
    assert cli.main(["serve", "--demo"]) == 0
    assert seen == {"host": "127.0.0.1", "port": 8765}


def test_receipts_are_empty_for_a_turn_with_no_plan(demo_app):
    client, _, _ = demo_app
    turn = client.post("/chat", json={"message": demo.CANNED_QUESTION}).json()
    assert client.get(f"/turns/{turn['turn_id']}/receipts").json()["receipts"] == []
    assert client.get("/turns/nope/receipts").status_code == 404
