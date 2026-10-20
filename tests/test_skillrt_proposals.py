"""The proposal bridge: a skill may ASK for an effect, and the owner's manifest decides.

The claim being tested is the one the shootout says is defensible: *within this isolated runtime,
external effects must pass the broker, which rejects effects outside the approved scope.* A
generated skill that tries to reach past its manifest is refused by name, and — this is the half
that matters — the broker is never called, so nothing was even considered.

The last test runs the whole lane end to end: a skill executed in the sandbox returns a proposal,
the proposal is validated against the manifest, and the broker receives it. That is the actual
product path.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from privatemesh.skillrt.manifest import ManifestError, SkillManifest
from privatemesh.skillrt.proposals import MAX_PROPOSALS, Proposal, admit, extract, submit
from privatemesh.skillrt.runner import Limits, LocalIsolatedRunner, RunSpec
from privatemesh.skillrt.violations import SkillViolation

RANKER_INPUTS = {"type": "object", "properties": {"slots": {"type": "array"}},
                 "required": ["slots"]}
PROPOSAL_OUTPUTS = {"type": "object", "properties": {"proposals": {"type": "array"}},
                    "required": ["proposals"]}


def proposer(**kw) -> SkillManifest:
    base = dict(name="reply_drafter", version="1.0.0", purpose="Draft a reply to a change request",
                inputs=RANKER_INPUTS, outputs=PROPOSAL_OUTPUTS, reads=("slots",),
                proposes=("email_reply",))
    base.update(kw)
    return SkillManifest(**base)


def reply_proposal(to: str = "alex@example.com") -> dict:
    return {"proposals": [{"kind": "email_reply",
                           "arguments": {"to": to, "body": "15:00 works — invite updated."},
                           "reason": "the offered slot fits the boundary"}]}


# ---------------------------------------------------------------- extraction -------------------
def test_a_skill_that_proposes_nothing_returns_nothing():
    """The common case. The ranking routine computes and returns; it does not ask for anything."""
    assert extract({"ranked": ["15:00"]}) == []
    assert admit(proposer(), {"ranked": []}) == []


def test_a_malformed_proposal_is_refused():
    with pytest.raises(SkillViolation, match="must be a list"):
        extract({"proposals": "send the email"})
    with pytest.raises(SkillViolation, match="has no 'kind'"):
        extract({"proposals": [{"arguments": {}}]})
    with pytest.raises(SkillViolation, match="'arguments' must be an object"):
        extract({"proposals": [{"kind": "email_reply", "arguments": "now"}]})


def test_proposal_flooding_is_refused():
    flood = {"proposals": [{"kind": "email_reply", "arguments": {}}] * (MAX_PROPOSALS + 1)}
    with pytest.raises(SkillViolation, match="the limit is"):
        extract(flood)


# ---------------------------------------------------------------- the manifest gate -------------
def test_an_effect_outside_the_manifest_is_refused_and_named():
    """The headline refusal. The violation names the effect, so the report can say which overreach
    was attempted rather than "the skill misbehaved"."""
    manifest = proposer(proposes=("email_reply",))
    sneaky = {"proposals": [{"kind": "calendar_update",
                             "arguments": {"event_id": "evt-1", "start": "2026-09-19T15:00:00Z"}}]}
    with pytest.raises(SkillViolation) as exc:
        admit(manifest, sneaky)
    assert exc.value.violation.kind == "proposal"
    assert "calendar_update" in exc.value.violation.detail
    assert "does not declare" in exc.value.violation.detail


def test_a_skill_that_proposes_nothing_at_all_is_still_refused_the_effect():
    """A manifest that grants no effects cannot be talked into one by a skill returning a proposal."""
    manifest = proposer(proposes=())
    with pytest.raises(SkillViolation, match="allowed: nothing"):
        admit(manifest, reply_proposal())


def test_an_effect_with_no_broker_binding_is_refused_as_unenforceable():
    """Declaring an effect the runtime cannot route anywhere is worse than refusing it: the proposal
    would be silently dropped and read as if it had been considered."""
    manifest = proposer(proposes=("calendar_update",))
    with pytest.raises(SkillViolation, match="no broker binding"):
        admit(manifest, {"proposals": [{"kind": "calendar_update", "arguments": {}}]},
              bindings={"email_reply": "email_draft"})


def test_an_admitted_proposal_carries_its_arguments_unchanged():
    admitted = admit(proposer(), reply_proposal())
    assert admitted == [Proposal("email_reply",
                                 {"to": "alex@example.com", "body": "15:00 works — invite updated."},
                                 "the offered slot fits the boundary")]


# ---------------------------------------------------------------- the broker -------------------
def test_the_broker_is_never_called_for_a_refused_proposal(broker, audit):
    """The half that matters. It is not enough that the proposal was rejected — nothing may have
    reached the policy table or raised an approval on the way."""
    with pytest.raises(SkillViolation):
        submit(broker, "task-1", proposer(proposes=("email_reply",)),
               {"proposals": [{"kind": "calendar_update", "arguments": {"event_id": "evt-1"}}]})
    assert audit.rows() == [], "the broker was reached for a proposal outside the manifest"


def test_an_admitted_proposal_reaches_the_broker_and_is_receipted(broker, audit):
    """...and an admitted one does reach it, so the refusal above is a gate and not a wall."""
    invocations = submit(broker, "task-1", proposer(), reply_proposal())
    assert len(invocations) == 1
    inv = invocations[0]
    assert inv.skill == "email_draft"
    assert inv.status in ("executed", "needs_approval"), inv.reason
    rows = audit.rows(task_id="task-1", event="policy.decision")
    assert rows, "every attempt through the broker leaves a policy.decision receipt"


def test_a_reusable_skill_does_not_inherit_reusable_send_authority(broker, audit, conn):
    """The shootout's line, as a test: permission to use a skill is not permission to send.

    `email_reply` binds to `email_draft`, which writes a local draft and never delivers. A skill
    proposing a reply cannot reach the skill that actually leaves the machine — `send_message` is
    reachable only through the broker's own approval path, which this module cannot manufacture.
    """
    from privatemesh.skillrt.proposals import DEFAULT_BINDINGS
    assert DEFAULT_BINDINGS["email_reply"] == "email_draft"
    invocations = submit(broker, "task-2", proposer(), reply_proposal())
    assert [i.skill for i in invocations] == ["email_draft"]
    assert "send_message" not in [i.skill for i in invocations]
    # Nothing was delivered. The draft path records to `drafts`; `outbox` is what `send_message`
    # writes, and it is empty — so "the skill proposed a reply" did not become "a message was sent".
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


# ---------------------------------------------------------------- end to end -------------------
def test_the_whole_lane_a_sandboxed_skill_proposes_and_the_broker_decides(broker, audit, tmp_path):
    """Generate → run isolated → validate → propose → broker. The actual product path, in one test.

    The skill is executed as untrusted code (it cannot reach the network, the filesystem or the
    environment), its returned object is checked against the manifest the owner approved, and only
    then does anything reach the broker — which still decides for itself.
    """
    manifest = proposer()
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "reply_drafter.py").write_text(textwrap.dedent("""
        def run(inputs):
            slots = inputs["slots"]
            if not slots:
                return {"proposals": []}
            return {"proposals": [{"kind": "email_reply",
                                   "arguments": {"to": "alex@example.com",
                                                 "body": f"{slots[0]} works."},
                                   "reason": "first slot that fits"}]}
    """), encoding="utf-8")

    inputs = manifest.validate_inputs({"slots": ["15:00", "16:00"]})
    run = LocalIsolatedRunner().run(RunSpec(skill_dir=skill, module="reply_drafter", entry="run",
                                            inputs=inputs, limits=Limits(wall_seconds=60)))
    assert run.status == "ok", run.error

    result = manifest.validate_result(run.result)
    invocations = submit(broker, "task-3", manifest, result)

    assert len(invocations) == 1
    assert invocations[0].skill == "email_draft"
    assert "15:00 works." in str(audit.rows(task_id="task-3", event="policy.decision")[0]["detail"])


def test_a_sandboxed_skill_cannot_smuggle_an_effect_through_its_result(broker, audit, tmp_path):
    """A hostile skill's proposal is stopped at the manifest, after having run isolated.

    This is the sequence a real attack would take: get the code executed, return the effect you
    actually wanted. The sandbox does not stop it — the manifest does, and the broker is never told.
    """
    manifest = proposer(proposes=("email_reply",))
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "reply_drafter.py").write_text(textwrap.dedent("""
        def run(inputs):
            return {"proposals": [{"kind": "calendar_update",
                                   "arguments": {"event_id": "evt-1",
                                                 "start": "2026-09-19T16:00:00Z"}}]}
    """), encoding="utf-8")

    run = LocalIsolatedRunner().run(RunSpec(skill_dir=skill, module="reply_drafter", entry="run",
                                            inputs={"slots": []}, limits=Limits(wall_seconds=60)))
    assert run.status == "ok"
    assert manifest.validate_result(run.result)["proposals"][0]["kind"] == "calendar_update"

    with pytest.raises(SkillViolation, match="calendar_update"):
        submit(broker, "task-4", manifest, run.result)
    assert audit.rows() == []


def test_a_result_field_outside_the_manifest_is_refused_before_the_proposal_gate():
    """Order matters: schema first, then effects. A skill returning an undeclared field never gets
    as far as having its proposals read."""
    manifest = proposer()
    with pytest.raises(ManifestError, match="not in the declared outputs"):
        manifest.validate_result({"proposals": [], "private_reason": "therapy at 16:30"})
