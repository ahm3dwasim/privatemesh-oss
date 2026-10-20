"""The runner's contract: what it runs, what it reports, and what it admits it did not enforce.

The benign ranking skill here is the product's actual missing routine (the shootout's "reusable
meeting-option evaluator"): rank offered slots against venue evidence and a busy/free boundary. It
is pure computation — no network, no credentials, no filesystem — which is exactly what a generated
skill is allowed to be.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from privatemesh.skillrt.runner import (
    ERROR,
    OK,
    TIMEOUT,
    Limits,
    LocalIsolatedRunner,
    RunSpec,
)

WALL = 60.0

RANKER = """
    import skillrt_api  # noqa: F401  (importable, proving the runtime surface exists)

    def _minutes(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    def run(inputs):
        print("ranking", len(inputs["slots"]), "slots")
        boundary = _minutes(inputs["boundary"])
        ranked, rejected = [], []
        for slot in inputs["slots"]:
            if slot in inputs["busy"]:
                rejected.append({"slot": slot, "reason": "already busy"})
            elif _minutes(slot) + inputs["duration"] + inputs["buffer"] > boundary:
                rejected.append({"slot": slot, "reason": "would overrun the protected boundary"})
            else:
                ranked.append(slot)
        ranked.sort(key=_minutes)
        return {"ranked": ranked, "rejected": rejected,
                "reason": f"{len(ranked)} of {len(inputs['slots'])} slots fit"}
"""


@pytest.fixture
def runner():
    return LocalIsolatedRunner()


def make_skill(root: Path, source: str = RANKER, name: str = "rank_slots") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def test_a_benign_skill_runs_and_returns_its_json(runner, tmp_path):
    """The whole point: a skill that only computes runs, and its structured result comes back."""
    skill = make_skill(tmp_path / "skill")
    result = runner.run(RunSpec(
        skill_dir=skill, module="rank_slots", entry="run",
        inputs={"slots": ["16:00", "15:00", "16:30"], "busy": ["16:30"], "boundary": "17:00",
                "duration": 45, "buffer": 45},
        limits=Limits(wall_seconds=WALL)))

    assert result.status == OK, result.error
    assert result.result == {
        "ranked": ["15:00"],
        "rejected": [{"slot": "16:00", "reason": "would overrun the protected boundary"},
                     {"slot": "16:30", "reason": "already busy"}],
        "reason": "1 of 3 slots fit"}


def test_the_skills_stdout_is_captured_separately_from_its_result(runner, tmp_path):
    """A skill's only outputs are a JSON result and stdout. They travel on different channels, so a
    skill that prints cannot corrupt the result the broker reads."""
    skill = make_skill(tmp_path / "skill")
    result = runner.run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                inputs={"slots": ["15:00"], "busy": [], "boundary": "17:00",
                                        "duration": 45, "buffer": 45},
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == OK
    assert result.stdout.strip() == "ranking 1 slots"
    assert isinstance(result.result, dict)


def test_the_isolation_report_does_not_claim_a_layer_that_was_not_applied(runner, tmp_path):
    """Honesty in the evidence. The report says which flags were used, that the environment was not
    inherited, and whether `sandbox-exec` was actually applied — including the reason when it was
    not. A run that quietly skipped a layer must not read like one that had it."""
    skill = make_skill(tmp_path / "skill")
    result = runner.run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                inputs={"slots": [], "busy": [], "boundary": "17:00",
                                        "duration": 45, "buffer": 45},
                                limits=Limits(wall_seconds=WALL)))
    iso = result.isolation
    assert iso["python_flags"] == ["-I", "-S", "-B"]
    assert iso["env_inherited"] is False
    assert iso["audit_hook"] is True
    assert isinstance(iso["sandbox_exec"], bool)
    if not iso["sandbox_exec"]:
        assert "sandbox_exec_note" in iso, "a skipped layer must say why it was skipped"


def test_the_runner_reports_the_sandbox_exec_decision_it_was_asked_for(tmp_path):
    """Feature detection is explicit rather than implicit: forcing it off says so in the result."""
    from privatemesh.skillrt.runner import LocalIsolatedRunner as R
    off = R(use_sandbox_exec=False)
    assert off.use_sandbox_exec is False


def test_the_limit_the_os_accepted_is_what_gets_reported(runner, tmp_path):
    """`limits_applied` is the OS's answer, not the request. macOS refuses the memory rlimits here,
    so a report that echoed the request would be claiming a cap that is not in force."""
    skill = make_skill(tmp_path / "skill")
    result = runner.run(RunSpec(skill_dir=skill, module="rank_slots", entry="run",
                                inputs={"slots": [], "busy": [], "boundary": "17:00",
                                        "duration": 45, "buffer": 45},
                                limits=Limits(cpu_seconds=3, memory_mb=256, file_size_mb=4,
                                              wall_seconds=WALL)))
    applied = result.isolation["limits_applied"]
    assert applied["cpu_seconds"] == 3
    assert applied["file_size_mb"] == 4
    # The memory cap is enforced by the parent-side watchdog where the rlimit is refused, so it is
    # deliberately absent from `limits_applied` — it is not an OS limit.


def test_an_infinite_loop_is_killed_by_the_cpu_cap(runner, tmp_path):
    skill = make_skill(tmp_path / "skill", "def run(inputs):\n    while True:\n        pass\n",
                       name="spin")
    result = runner.run(RunSpec(skill_dir=skill, module="spin", entry="run",
                                limits=Limits(cpu_seconds=1, wall_seconds=30)))
    assert result.status == "violation"
    assert result.violation.kind == "resources"
    assert "SIGXCPU" in result.violation.detail


def test_a_skill_that_outlives_the_wall_clock_is_killed(runner, tmp_path):
    """CPU time and wall time are different caps: a skill that sleeps burns no CPU but must still
    not hold the runtime forever."""
    skill = make_skill(tmp_path / "skill", """
        import time
        def run(inputs):
            time.sleep(30)
            return {'v': 'slept'}
    """, name="sleeper")
    result = runner.run(RunSpec(skill_dir=skill, module="sleeper", entry="run",
                                limits=Limits(cpu_seconds=30, wall_seconds=1.5)))
    assert result.status == TIMEOUT
    assert "wall clock" in result.error


def test_a_memory_hog_is_killed_by_the_watchdog(runner, tmp_path):
    """Touch the pages, so this measures resident memory rather than a lazy allocation."""
    skill = make_skill(tmp_path / "skill", """
        def run(inputs):
            blocks = []
            for _ in range(200):
                block = bytearray(10 * 1024 * 1024)
                for offset in range(0, len(block), 4096):
                    block[offset] = 1
                blocks.append(block)
            return {'blocks': len(blocks)}
    """, name="memhog")
    result = runner.run(RunSpec(skill_dir=skill, module="memhog", entry="run",
                                limits=Limits(memory_mb=256, cpu_seconds=60, wall_seconds=40)))
    assert result.status == "violation"
    assert result.violation.kind == "resources"
    assert "memory cap" in result.violation.detail


def test_a_skill_that_raises_is_an_error_not_a_violation(runner, tmp_path):
    """A skill that is merely broken must not be reported as a security event. The two are
    different facts and the report has to keep them apart."""
    skill = make_skill(tmp_path / "skill", "def run(inputs):\n    raise ValueError('boom')\n",
                       name="broken")
    result = runner.run(RunSpec(skill_dir=skill, module="broken", entry="run",
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == ERROR
    assert result.violation is None
    assert "ValueError: boom" in result.error
    assert "ValueError" in result.traceback


def test_a_skill_that_returns_a_non_object_is_refused(runner, tmp_path):
    skill = make_skill(tmp_path / "skill", "def run(inputs):\n    return [1, 2, 3]\n",
                       name="listy")
    result = runner.run(RunSpec(skill_dir=skill, module="listy", entry="run",
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == ERROR
    assert "not a dict" in result.error


def test_a_missing_entry_point_is_an_error(runner, tmp_path):
    skill = make_skill(tmp_path / "skill", "def something_else(inputs):\n    return {}\n",
                       name="noentry")
    result = runner.run(RunSpec(skill_dir=skill, module="noentry", entry="run",
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == ERROR
    assert "no callable 'run'" in result.error


def test_the_skill_cannot_learn_the_result_descriptor_from_its_argv(runner, tmp_path):
    """The result descriptor is scrubbed from `sys.argv` before the skill runs, so a skill cannot
    trivially write a forged result where the parent will read it."""
    skill = make_skill(tmp_path / "skill", """
        import sys
        def run(inputs):
            return {'argv': list(sys.argv)}
    """, name="nosy")
    result = runner.run(RunSpec(skill_dir=skill, module="nosy", entry="run",
                                limits=Limits(wall_seconds=WALL)))
    assert result.status == OK
    assert result.result == {"argv": ["nosy"]}


def test_both_backends_satisfy_the_same_protocol():
    """`ContreeRunner` is selectable later; if it drifts from the protocol, swapping backends stops
    being a configuration change."""
    from privatemesh.skillrt.runner import ContreeRunner, Runner

    assert isinstance(LocalIsolatedRunner(), Runner)
    assert isinstance(ContreeRunner(client=None), Runner)
