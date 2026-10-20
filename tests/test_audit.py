"""The audit trail: append-only, hash-chained, and loud when it cannot write."""
from __future__ import annotations

import pytest

from privatemesh.audit import GENESIS, AuditError, AuditLog, PolicyRecord, row_hash


def test_the_chain_links_every_row_to_its_predecessor(audit):
    audit.record("a", task_id="t", detail={"n": 1})
    audit.record("b", task_id="t", detail={"n": 2})
    rows = audit.rows()
    assert rows[0]["prev_hash"] == GENESIS
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    assert audit.verify().ok is True


def test_verify_catches_a_tampered_row(audit):
    audit.record("a", task_id="t")
    audit.record("b", task_id="t")
    audit.record("c", task_id="t")
    audit._conn.execute("UPDATE audit SET detail='{\"forged\":true}' WHERE id=2")
    audit._conn.commit()

    v = audit.verify()
    assert v.ok is False and v.broken_id == 2
    assert "modified after it was written" in v.note


def test_verify_catches_a_deleted_row(audit):
    audit.record("a", task_id="t")
    audit.record("b", task_id="t")
    audit.record("c", task_id="t")
    audit._conn.execute("DELETE FROM audit WHERE id=2")
    audit._conn.commit()

    v = audit.verify()
    assert v.ok is False and v.broken_id == 3      # the first row that no longer chains
    assert "does not chain" in v.note


def test_verify_catches_a_row_appended_without_the_chain(audit):
    audit.record("a", task_id="t")
    audit._conn.execute(
        "INSERT INTO audit(id,ts,task_id,event,detail,prev_hash,hash) VALUES(2,'now','t','fake','{}',?,?)",
        (GENESIS, row_hash(GENESIS, {"id": 2, "ts": "now", "task_id": "t", "event": "fake",
                                     "detail": "{}"})))
    audit._conn.commit()
    assert audit.verify().broken_id == 2


def test_a_write_failure_raises_instead_of_being_swallowed():
    log = AuditLog(":memory:")
    log.record("ok")
    log._conn.close()
    with pytest.raises(AuditError, match="audit write failed"):
        log.record("will_fail")


def test_an_empty_event_name_is_refused(audit):
    with pytest.raises(AuditError):
        audit.record("")


def test_rows_can_be_filtered_by_task_and_event(audit):
    audit.record("a", task_id="t1")
    audit.record("b", task_id="t2")
    audit.record("a", task_id="t2")
    assert len(audit.rows(task_id="t2")) == 2
    assert len(audit.rows(event="a")) == 2
    assert len(audit.rows(task_id="t2", event="a")) == 1


def test_policy_record_persists_its_tri_state(audit):
    rec = PolicyRecord(task_id="t", tool="send_message", gate_name="SendMessage", tier="act",
                       risk="HIGH", sensitive=True, decision="needs_approval", reason="sensitive",
                       approved=True, executed=True, ok=True, duration_ms=12, detail="approval x")
    audit.record("policy.decision", task_id="t", detail=rec.as_row())
    detail = audit.rows()[0]["detail"]
    assert '"approved":true' in detail and '"executed":true' and '"duration_ms":12' in detail


def test_a_record_with_no_detail_still_chains(audit):
    audit.record("a")
    audit.record("b")
    assert audit.verify().ok is True
