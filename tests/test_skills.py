"""The built-in skills, over a mocked Tavily and a real in-memory store."""
from __future__ import annotations

import json

import httpx
import pytest

from privatemesh.skills_builtin import build_registry, make_web_research


def _tavily(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _call(registry, name, args, broker):
    """Skills are broker-gated, so a direct call is not how these are exercised."""
    return broker.invoke("t", name, args)


def test_web_research_returns_an_answer_and_its_sources(conn, audit):
    from privatemesh.broker import Broker

    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"answer": "Nemotron is an NVIDIA model family.",
                                         "results": [{"title": "Nemotron", "url": "https://nv.example/1"},
                                                     {"title": "Token Factory", "url": "https://nb.example/2"}]})

    reg = build_registry(conn=conn, tavily_key="tvly-test", http=_tavily(handler))
    inv = _call(reg, "web_research", {"query": "what is nemotron"}, Broker(reg, audit))

    assert inv.status == "executed" and inv.ok is True
    assert "Nemotron is an NVIDIA model family." in inv.content
    assert inv.meta["sources"] == ["https://nv.example/1", "https://nb.example/2"]
    assert seen["query"] == "what is nemotron" and seen["api_key"] == "tvly-test"


def test_web_research_reports_a_http_error_instead_of_raising(conn, audit):
    from privatemesh.broker import Broker

    reg = build_registry(conn=conn, tavily_key="tvly-test",
                         http=_tavily(lambda r: httpx.Response(429, json={})))
    inv = _call(reg, "web_research", {"query": "anything"}, Broker(reg, audit))
    assert inv.ok is False and "429" in (inv.error or "")


def test_web_research_refuses_a_query_that_looks_like_a_secret(conn, audit):
    """A search box is an egress path; a key pasted into one never leaves the machine."""
    from privatemesh.broker import Broker

    called = []
    reg = build_registry(conn=conn, tavily_key="tvly-test",
                         http=_tavily(lambda r: called.append(r) or httpx.Response(200, json={})))
    inv = _call(reg, "web_research", {"query": "is sk-ant-abcdefghijklmnop valid"}, Broker(reg, audit))
    assert inv.ok is False and "secret" in (inv.error or "")
    assert called == []


def test_web_research_without_a_key_says_so(conn, audit):
    from privatemesh.broker import Broker

    reg = build_registry(conn=conn, tavily_key="")
    inv = _call(reg, "web_research", {"query": "x"}, Broker(reg, audit))
    assert inv.ok is False and "TAVILY_API_KEY" in (inv.error or "")


def test_notes_round_trip_through_the_broker(conn, audit):
    from privatemesh.broker import Broker

    reg = build_registry(conn=conn, tavily_key="")
    broker = Broker(reg, audit)
    written = broker.invoke("t", "notes_write", {"text": "buy oat milk"})
    assert written.status == "executed" and written.ok is True

    read = broker.invoke("t", "notes_read", {"query": "oat"})
    assert read.ok is True and "buy oat milk" in read.content
    assert broker.invoke("t", "notes_read", {"query": "unrelated"}).content == "(no notes)"


def test_an_email_draft_is_never_sent(conn, audit):
    from privatemesh.broker import Broker

    reg = build_registry(conn=conn, tavily_key="")
    broker = Broker(reg, audit)
    inv = broker.invoke("t", "email_draft",
                        {"to": "alex@example.com", "subject": "hi", "body": "see you Friday"})

    assert inv.status == "executed" and inv.ok is True      # DRAFT tier: automatic
    assert inv.meta["sent"] is False
    assert "see you Friday" in inv.content
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_a_draft_still_needs_a_recipient_and_a_body(conn, audit):
    from privatemesh.broker import Broker

    reg = build_registry(conn=conn, tavily_key="")
    inv = Broker(reg, audit).invoke("t", "email_draft", {"to": "alex@example.com"})
    # Refused at the broker, before execution and before any approval (the skill never ran).
    assert inv.ok is None and inv.status == "denied" and "body" in (inv.error or "")


def test_the_fake_send_records_the_transport_it_used(conn, audit):
    from privatemesh.broker import Broker

    reg = build_registry(conn=conn, tavily_key="")
    broker = Broker(reg, audit)
    inv = broker.invoke("t", "send_message", {"to": "alex", "body": "hi"})
    broker.decide(inv.approval_id, approve=True)

    row = conn.execute("SELECT transport, status FROM outbox").fetchone()
    assert row["transport"] == "fake-local-outbox" and row["status"] == "recorded"


def test_a_skill_is_only_usable_through_the_broker(conn):
    reg = build_registry(conn=conn, tavily_key="")
    with pytest.raises(PermissionError):
        reg.require("notes_write").fn({"text": "bypass"})
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 0


def test_web_research_is_declared_retriable_and_the_others_are_not(conn):
    reg = build_registry(conn=conn, tavily_key="")
    assert reg.is_retriable("web_research") is True
    assert reg.is_retriable("send_message") is False
    assert reg.is_retriable("notes_write") is False


def test_make_web_research_builds_without_a_client(monkeypatch):
    tool = make_web_research(None, "k")
    assert tool.scope == "web.read" and tool.gate_name == "WebSearch"
