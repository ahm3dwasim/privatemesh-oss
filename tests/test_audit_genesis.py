import os

from privatemesh.audit import AuditLog


def test_a_fresh_empty_log_verifies_clean(tmp_path):
    """A first run must not greet the owner with a red 'tampered' banner."""
    log = AuditLog(str(tmp_path / "a.db"), audit_key="k")
    assert log.verify().ok


def test_a_log_with_rows_whose_checkpoint_vanished_still_fails(tmp_path):
    db = tmp_path / "a.db"
    log = AuditLog(str(db), audit_key="k")
    log.record("x")
    log.record("y")
    os.remove(log.checkpoint.path)
    reopened = AuditLog(str(db), audit_key="k")     # reopening must NOT silently re-witness it
    assert not reopened.verify().ok
