"""Memory: consent is what makes a memory reachable, and forgetting is observable."""
from __future__ import annotations

import datetime as _dt

from privatemesh import memory as mem


def _write(conn, text, **kw):
    return mem.write_memory(conn, type=kw.pop("type", "preference"), text=text, **kw)


def test_unapproved_memory_is_never_recalled(conn):
    """The whole consent model in one assertion: written, valid, matching — and invisible."""
    mem_id = _write(conn, "I take my coffee black", approved=False)
    assert mem_id > 0
    assert mem.recall(conn, "coffee") == []

    assert mem.approve(conn, mem_id) is True
    hits = mem.recall(conn, "coffee")
    assert [h.id for h in hits] == [mem_id]
    assert hits[0].approved is True


def test_revoking_consent_hides_it_again(conn):
    mem_id = _write(conn, "my flight is on Friday", approved=True)
    assert [h.id for h in mem.recall(conn, "flight")] == [mem_id]
    mem.approve(conn, mem_id, approved=False)
    assert mem.recall(conn, "flight") == []


def test_forgotten_memory_is_invalidated_not_deleted(conn):
    mem_id = _write(conn, "I drive a blue car", approved=True)
    hits = mem.forget_matching(conn, "blue car")
    assert [h.id for h in hits] == [mem_id]
    # bi-temporal: the row survives, with invalid_at set — the history is the proof.
    assert mem.recall(conn, "blue car") == []
    assert mem.get_memory(conn, mem_id) is not None


def test_forget_by_id_is_also_bitemporal(conn):
    mem_id = _write(conn, "prefers window seats", approved=True)
    assert mem.forget_id(conn, mem_id) is True
    assert mem.recall(conn, "window seats") == []
    assert mem.forget_id(conn, mem_id) is False  # already invalid — not double-counted
    assert mem.get_memory(conn, mem_id).text == "prefers window seats"


def test_forget_ignores_memories_that_were_never_approved(conn):
    _write(conn, "pending thought about llamas", approved=False)
    assert mem.forget_matching(conn, "llamas") == []


def test_supersede_records_what_replaced_what(conn):
    old = _write(conn, "I am 23 years old", approved=True)
    new = _write(conn, "I am 24 years old", approved=True)
    assert mem.supersede(conn, old, new) is True
    assert mem.supersede(conn, old, new) is False       # already superseded, once only
    assert [h.id for h in mem.recall(conn, "years old")] == [new]


def test_secrets_are_scrubbed_before_they_are_stored(conn, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "sk-supersecret-value-1234567890")
    mem_id = _write(conn, "my key is sk-supersecret-value-1234567890 ok", approved=True)
    stored = mem.get_memory(conn, mem_id).text
    assert "sk-supersecret-value-1234567890" not in stored
    assert "[REDACTED]" in stored


def test_recall_ranking_prefers_important_and_recent(conn):
    now = _dt.datetime.now()
    old_ts = (now - _dt.timedelta(days=900)).isoformat(timespec="seconds")
    low = _write(conn, "project atlas status is green", importance=1, ts=old_ts, approved=True)
    high = _write(conn, "project atlas deadline is Friday", importance=5,
                  ts=now.isoformat(timespec="seconds"), approved=True)
    hits = mem.recall(conn, "project atlas", k=2)
    assert hits[0].id == high and low in [h.id for h in hits]


def test_counts_report_pending_consent(conn):
    _write(conn, "one", approved=True)
    _write(conn, "two", approved=False)
    counts = mem.counts_by_type(conn)
    assert counts["total"] == 2 and counts["pending"] == 1


def test_recall_with_no_usable_terms_is_empty(conn):
    _write(conn, "anything at all", approved=True)
    assert mem.recall(conn, "the and of") == []
    assert mem.recall(conn, "") == []
