"""LOW 8 — memory writes through the API go through one receipted command layer.

The review's finding was narrow and correct: `/memories` called `mem.write_memory()` and audited
afterwards, so a failed audit write left a memory row that nothing had a receipt for, and the
caller got an error describing a change that had actually happened. Same for consent and forget.

The fix routes every owner-facing memory mutation through `MemoryCommands`, which reserves an
intent receipt first (and refuses to mutate if it cannot), then records the outcome.
"""
from __future__ import annotations

import json

import pytest
from conftest import S, fake_llm, make_agent
from fastapi.testclient import TestClient

from privatemesh import memory as mem
from privatemesh.api import build_app
from privatemesh.audit import AuditError, AuditLog
from privatemesh.broker import Broker
from privatemesh.commands import MemoryCommands
from privatemesh.grants import GrantStore


@pytest.fixture
def client(conn, registry, audit, grants):
    broker = Broker(registry, audit, grants=grants)
    tf = fake_llm(intent={"action": "answer", "query": "coffee", "steps": 0})
    agent = make_agent(conn, registry, audit, broker, tf)
    return TestClient(build_app(agent=agent, conn=conn, audit=audit, broker=broker, grants=grants,
                                llm=tf.tf, registry=registry, settings=S))


def _events(audit):
    return [r["event"] for r in audit.rows()]


def test_a_memory_written_through_the_api_is_receipted_before_it_is_stored(client, audit):
    created = client.post("/memories", json={"text": "I take my coffee black",
                                             "type": "preference"})
    assert created.status_code == 200

    events = _events(audit)
    assert "memory.intent" in events and "memory.outcome" in events
    assert events.index("memory.intent") < events.index("memory.outcome")

    intent = audit.rows(event="memory.intent")[0]
    outcome = audit.rows(event="memory.outcome")[0]
    assert json.loads(intent["detail"])["op"] == "write"
    assert json.loads(outcome["detail"])["intent_id"] == intent["id"]
    assert json.loads(outcome["detail"])["memory_id"] == created.json()["id"]


def test_consent_and_forget_are_receipted_the_same_way(client, audit):
    memory_id = client.post("/memories", json={"text": "I take my coffee black"}).json()["id"]
    client.post(f"/memories/{memory_id}/approve")
    client.post(f"/memories/{memory_id}/forget")

    ops = [json.loads(r["detail"])["op"] for r in audit.rows(event="memory.intent")]
    assert ops == ["write", "consent", "forget"]
    # each intent is paired with an outcome that names it
    intents = [r["id"] for r in audit.rows(event="memory.intent")]
    outcomes = [json.loads(r["detail"]) for r in audit.rows(event="memory.outcome")]
    assert [o["intent_id"] for o in outcomes] == intents
    assert [o["changed"] for o in outcomes if o["op"] == "forget"] == [True]


def test_the_outcome_records_that_nothing_was_stored_rather_than_going_quiet(client, audit):
    """"Nothing worth storing" is a real answer, and the receipt says so — it must not look the
    same as a write that broke."""
    response = client.post("/memories", json={"text": "sk-ant-api03-" + "A" * 40})
    assert response.status_code == 400
    outcome = json.loads(audit.rows(event="memory.outcome")[0]["detail"])
    assert outcome["stored"] is False and outcome["memory_id"] == 0


def test_an_unwritable_intent_receipt_stops_the_memory_write(conn, audit):
    """The ordering guarantee, checked where it matters: no receipt, no mutation."""
    def boom(*a, **kw):
        raise AuditError("the receipt store is down")

    audit.record = boom
    with pytest.raises(AuditError):
        MemoryCommands(conn, audit).write(type="preference", text="must not be stored")
    assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0


def test_the_intent_receipt_exists_before_the_memory_row_is_written(conn, audit, monkeypatch):
    """Taken from inside the write itself: at the moment the row is inserted, its reservation is
    already on the chain."""
    from privatemesh import commands as cmds

    observed: dict = {}
    real = mem.write_memory

    def spy(*a, **kw):
        observed["intents_at_write"] = len(audit.rows(event="memory.intent"))
        observed["outcomes_at_write"] = len(audit.rows(event="memory.outcome"))
        return real(*a, **kw)

    monkeypatch.setattr(cmds.mem, "write_memory", spy)
    new_id = MemoryCommands(conn, audit).write(type="preference", text="I take my coffee black")

    assert new_id and observed == {"intents_at_write": 1, "outcomes_at_write": 0}


def test_the_api_and_the_command_layer_agree_on_what_was_written(conn, registry, audit, grants):
    """The endpoints are a thin view onto the command layer — no second implementation of the
    rule, and the same audit trail either way."""
    broker = Broker(registry, audit, grants=grants)
    tf = fake_llm(intent={"action": "answer", "query": "coffee", "steps": 0})
    agent = make_agent(conn, registry, audit, broker, tf)
    client = TestClient(build_app(agent=agent, conn=conn, audit=audit, broker=broker,
                                  grants=grants, llm=tf.tf, registry=registry, settings=S))

    memory_id = client.post("/memories", json={"text": "I take my coffee black",
                                               "type": "preference"}).json()["id"]
    assert mem.get_memory(conn, memory_id) is not None
    assert client.post(f"/memories/{memory_id}/approve").json() == {"id": memory_id,
                                                                    "approved": True}
    assert mem.get_memory(conn, memory_id).approved is True
    assert client.post(f"/memories/{memory_id}/forget").json()["forgotten"] is True
    assert mem.get_memory(conn, memory_id).text == "I take my coffee black"


def test_an_unknown_memory_is_still_a_404_and_is_not_receipted_as_a_change(client, audit):
    response = client.post("/memories/9999/approve")
    assert response.status_code == 404
    outcome = json.loads(audit.rows(event="memory.outcome")[0]["detail"])
    assert outcome["changed"] is False


def test_the_command_layer_shares_the_audit_chain_with_everything_else(client, audit):
    """Memory receipts are links in the same chain, not a side log — so `verify()` covers them."""
    client.post("/memories", json={"text": "I take my coffee black"})
    assert isinstance(audit, AuditLog)
    assert audit.verify().ok is True
