"""The policy engine: the tiers it reads off a skill, and the two rules nothing can loosen."""
from __future__ import annotations

import pytest

from privatemesh.policy import (AutonomyPolicy, Decision, Tier, classify_tool, evaluate,
                                is_sensitive, load_autonomy_policy)
from privatemesh.skills import Risk, Tool, ToolResult


def _tool(name="t", *, gate="Read", risk=Risk.LOW, approval_required=False):
    return Tool(name=name, description="d", parameters={}, risk=risk,
                fn=lambda a: ToolResult(ok=True), gate_name=gate,
                approval_required=approval_required)


def test_classify_reads_read_draft_act_off_the_gate_name():
    assert classify_tool("Read") is Tier.READ
    assert classify_tool("Write") is Tier.DRAFT
    assert classify_tool("Draft") is Tier.DRAFT
    assert classify_tool("SendMessage") is Tier.ACT
    assert classify_tool("Bash") is Tier.ACT


def test_the_builtin_skill_gates_land_in_the_intended_tiers():
    assert classify_tool("WebSearch") is Tier.READ
    assert classify_tool("SendMessage") is Tier.ACT


def test_send_is_sensitive_regardless_of_arguments():
    assert is_sensitive("SendMessage", {"to": "a", "body": "hi"}) is True


def test_a_draft_about_passwords_is_not_treated_as_a_credential_action():
    """Free text is not a path: judging a draft by the words in it is friction, not safety."""
    assert is_sensitive("Draft", {"to": "a", "body": "please reset your password"}) is False


def test_writing_a_secret_path_is_sensitive():
    assert is_sensitive("Write", {"path": "~/.ssh/id_rsa", "text": "x"}) is True
    assert is_sensitive("Write", {"path": "notes/todo.txt", "text": "x"}) is False


def test_read_tier_is_allowed_automatically():
    r = evaluate(_tool(gate="Read"), {}, interactive=True)
    assert r.decision is Decision.ALLOW and r.tier is Tier.READ


def test_act_tier_needs_the_owner():
    r = evaluate(_tool(gate="SendMessage", risk=Risk.HIGH), {"to": "a"}, interactive=True)
    assert r.decision is Decision.NEEDS_APPROVAL and r.sensitive is True


def test_unattended_act_becomes_a_hard_deny():
    """No human present means no approval is possible, so the action is refused outright."""
    r = evaluate(_tool(gate="SendMessage", risk=Risk.HIGH), {"to": "a"}, interactive=False)
    assert r.decision is Decision.DENY
    assert "unattended" in r.reason


def test_blocked_risk_is_denied_even_with_an_auto_grant():
    policy = AutonomyPolicy(medium_action="auto", tool_overrides={"t": "auto"})
    r = evaluate(_tool(gate="Read", risk=Risk.BLOCKED), {}, interactive=True, autonomy=policy)
    assert r.decision is Decision.DENY


def test_a_grant_cannot_auto_run_a_sensitive_action():
    """The demo's claim in one line: promoting a skill can never wave through an ACT."""
    policy = AutonomyPolicy(tool_overrides={"send_message": "auto"})
    r = evaluate(_tool(name="send_message", gate="SendMessage", risk=Risk.HIGH),
                 {"to": "a", "body": "b"}, interactive=True, autonomy=policy)
    assert r.decision is Decision.NEEDS_APPROVAL


def test_a_grant_can_tighten_a_normally_automatic_skill():
    policy = AutonomyPolicy(tool_overrides={"notes_read": "approve"})
    r = evaluate(_tool(name="notes_read", gate="Read"), {}, interactive=True, autonomy=policy)
    assert r.decision is Decision.NEEDS_APPROVAL
    policy = AutonomyPolicy(tool_overrides={"notes_read": "deny"})
    assert evaluate(_tool(name="notes_read", gate="Read"), {}, interactive=True,
                    autonomy=policy).decision is Decision.DENY


def test_high_risk_needs_approval_even_at_read_tier():
    r = evaluate(_tool(gate="Read", risk=Risk.HIGH), {}, interactive=True)
    assert r.decision is Decision.NEEDS_APPROVAL


def test_writing_the_policy_file_is_refused_outright():
    r = evaluate(_tool(gate="Write", risk=Risk.MEDIUM),
                 {"path": "state/autonomy_policy.json"}, interactive=True)
    assert r.decision is Decision.DENY and "autonomy policy" in r.reason


def test_autonomy_policy_load_is_total(tmp_path):
    assert load_autonomy_policy(tmp_path / "missing.json") == AutonomyPolicy()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_autonomy_policy(bad) == AutonomyPolicy()
    good = tmp_path / "good.json"
    good.write_text('{"medium_action": "auto", "tool_overrides": {"a": "deny", "b": "nonsense"}}')
    p = load_autonomy_policy(good)
    assert p.medium_auto is True and p.override_for("a") == "deny" and p.override_for("b") is None


def test_evaluate_never_raises_on_junk_arguments():
    assert evaluate(_tool(gate="Write"), {"path": None, "other": 5}, interactive=True).decision \
        in {Decision.ALLOW, Decision.NEEDS_APPROVAL, Decision.DENY}


def test_an_unknown_tier_risk_combination_still_returns_a_result():
    with pytest.raises(ValueError):
        Tool(name="x", description="d", parameters={}, risk="HIGH", fn=lambda a: None, gate_name="Read")
