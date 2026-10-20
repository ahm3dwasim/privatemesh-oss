"""HIGH 2 — a deleted final row must not verify clean.

The review's probe recorded two rows, deleted the second, and got `ok=True, "1 rows verified
intact"`. A hash chain cannot see that on its own: every row commits to its predecessor, so an
interior deletion breaks the row after it, but the LAST row has nothing after it to notice it is
gone — and the last row is the receipt for whatever just happened.

The fix is an external witness: a signed `(row_count, tip_hash)` written beside the database on
every append, which `verify()` cross-checks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from privatemesh.audit import AUDIT_KEY_ENV, KEY_SUFFIX, AuditLog

CHECKPOINT = "audit.db.checkpoint"


def _log(tmp_path, **kw) -> AuditLog:
    return AuditLog(str(tmp_path / "audit.db"), **kw)


def test_deleting_the_final_row_is_detected(tmp_path):
    """The finding's exact probe, now failing instead of passing."""
    log = _log(tmp_path)
    log.record("a", task_id="t")
    log.record("b", task_id="t")
    assert log.verify().ok is True

    log._conn.execute("DELETE FROM audit WHERE id=2")
    log._conn.commit()

    verdict = log.verify()
    assert verdict.ok is False, "a deleted tip verified clean — the exact hole the review found"
    assert "checkpoint" in verdict.note
    assert "1 rows" in verdict.note or "truncated" in verdict.note


def test_a_tip_replaced_by_a_different_row_is_detected(tmp_path):
    """Not just removal — the newest row cannot be swapped for another one either."""
    log = _log(tmp_path)
    log.record("a", task_id="t")
    log.record("b", task_id="t")
    log._conn.execute("UPDATE audit SET detail='{\"forged\":true}' WHERE id=2")
    log._conn.commit()
    assert log.verify().ok is False


def test_a_missing_checkpoint_is_a_failure_not_a_silent_pass(tmp_path):
    """The witness is required. If it can simply be deleted, it witnesses nothing."""
    log = _log(tmp_path)
    log.record("a", task_id="t")
    os.remove(str(tmp_path / CHECKPOINT))

    verdict = log.verify()
    assert verdict.ok is False
    assert "no signed checkpoint" in verdict.note


def test_a_forged_checkpoint_is_refused_without_the_key(tmp_path):
    """The attacker's best move: truncate the store AND rewrite the witness to match.

    They can read the file, so they can produce a consistent-looking record — but they cannot
    sign it, because the HMAC key lives elsewhere (in `PM_AUDIT_KEY`, or a 0600 key file the
    repository does not contain). The signature is what makes the witness worth having."""
    path = str(tmp_path / "audit.db")
    log = AuditLog(path)
    log.record("a", task_id="t")
    log.record("b", task_id="t")
    surviving_tip = log.rows()[0]["hash"]

    log._conn.execute("DELETE FROM audit WHERE id=2")
    log._conn.commit()

    checkpoint = Path(path + ".checkpoint")
    forged = json.loads(checkpoint.read_text())
    forged["rows"], forged["tip"] = 1, surviving_tip      # consistent with the truncated store
    checkpoint.write_text(json.dumps(forged, sort_keys=True, separators=(",", ":")))

    verdict = log.verify()
    assert verdict.ok is False
    assert "HMAC" in verdict.note


def test_the_key_file_is_created_private_and_never_in_the_database(tmp_path, monkeypatch):
    """A witness the store can rewrite is not a witness, so the key must not live inside it."""
    monkeypatch.delenv(AUDIT_KEY_ENV, raising=False)
    log = _log(tmp_path)
    log.record("a", task_id="t")

    key_file = Path(f"{tmp_path / 'audit.db'}.checkpoint{KEY_SUFFIX}")
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600
    assert log._conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE '%key%'"
    ).fetchone()[0] == 0


def test_pm_audit_key_is_used_when_set(tmp_path, monkeypatch):
    """With the key supplied by the environment, no key file is written at all."""
    monkeypatch.setenv(AUDIT_KEY_ENV, "a-key-from-the-environment")
    log = _log(tmp_path)
    log.record("a", task_id="t")
    assert log.verify().ok is True
    assert not Path(f"{tmp_path / 'audit.db'}.checkpoint{KEY_SUFFIX}").exists()


def test_an_explicit_checkpoint_path_is_honoured(tmp_path):
    log = AuditLog(str(tmp_path / "audit.db"), checkpoint_path=str(tmp_path / "tip.witness"))
    log.record("a", task_id="t")
    assert Path(tmp_path / "tip.witness").exists()
    assert log.verify().ok is True


def test_a_reopened_log_still_verifies(tmp_path):
    """The witness is durable, not per-process: a restart must not look like tampering."""
    first = _log(tmp_path)
    first.record("a", task_id="t")
    first.record("b", task_id="t")

    second = _log(tmp_path)
    assert second.verify().ok is True
    second.record("c", task_id="t")
    assert second.verify().ok is True

    third = _log(tmp_path)
    assert third.verify().ok is True
    assert third.verify().note.endswith("signed checkpoint")


def test_the_interior_deletion_case_still_works_as_before(tmp_path):
    """The pre-existing guarantee is unchanged: the chain still names the FIRST broken row."""
    log = _log(tmp_path)
    for name in "abc":
        log.record(name, task_id="t")
    log._conn.execute("DELETE FROM audit WHERE id=2")
    log._conn.commit()
    verdict = log.verify()
    assert verdict.ok is False and verdict.broken_id == 3
    assert "does not chain" in verdict.note
