"""The harness: a generated skill is graded by tests it never saw, and cannot reach them.

A generator that writes the skill and writes its tests has proved nothing — it passes the tests it
wrote to pass. The evidence has to come from cases the coder never saw, and those cases have to be
unreachable by the code being graded. Every test here is about that second half.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from privatemesh.skillrt.harness import (
    HarnessError,
    HeldOutTestsInsideSkill,
    report_json,
    run_harness,
)
from privatemesh.skillrt.manifest import SkillManifest
from privatemesh.skillrt.runner import LocalIsolatedRunner

WALL = 60.0

RANKER = """
    def _minutes(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    def run(inputs):
        boundary = _minutes(inputs["boundary"])
        ranked = [s for s in inputs["slots"]
                  if s not in inputs["busy"]
                  and _minutes(s) + inputs["duration"] + inputs["buffer"] <= boundary]
        ranked.sort(key=_minutes)
        return {"ranked": ranked}
"""

#: Passes on the easy case the generator would have written for itself, and is WRONG on the case the
#: owner cares about: it ignores the buffer, so 16:00 looks like it fits when it does not.
BUFFER_BLIND = """
    def _minutes(hhmm):
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    def run(inputs):
        boundary = _minutes(inputs["boundary"])
        ranked = [s for s in inputs["slots"]
                  if s not in inputs["busy"]
                  and _minutes(s) + inputs["duration"] <= boundary]
        ranked.sort(key=_minutes)
        return {"ranked": ranked}
"""

MANIFEST = SkillManifest(
    name="rank_slots", version="1.0.0", purpose="Rank meeting slots around a protected boundary",
    inputs={"type": "object",
            "properties": {"slots": {"type": "array"}, "busy": {"type": "array"},
                           "boundary": {"type": "string"}, "duration": {"type": "integer"},
                           "buffer": {"type": "integer"}},
            "required": ["slots"]},
    outputs={"type": "object", "properties": {"ranked": {"type": "array"}}, "required": ["ranked"]},
    reads=("slots", "busy", "boundary", "duration", "buffer"), proposes=())

OWN_TEST = """
    import rank_slots

    def test_ranks_the_obvious_slot():
        out = rank_slots.run({"slots": ["15:00"], "busy": [], "boundary": "17:00",
                              "duration": 45, "buffer": 45})
        assert out["ranked"] == ["15:00"]
"""

HELD_OUT_TEST = """
    import rank_slots

    def test_the_buffer_is_part_of_the_boundary():
        out = rank_slots.run({"slots": ["16:00"], "busy": [], "boundary": "17:00",
                              "duration": 45, "buffer": 45})
        assert out["ranked"] == [], "16:00 plus 45 minutes plus the 45-minute buffer overruns 17:00"
"""


def build(tmp_path: Path, *, skill_source: str = RANKER) -> tuple[Path, Path, Path]:
    skill = tmp_path / "skill"
    (skill / "tests").mkdir(parents=True)
    (skill / "rank_slots.py").write_text(textwrap.dedent(skill_source), encoding="utf-8")
    own = skill / "tests"
    (own / "test_own.py").write_text(textwrap.dedent(OWN_TEST), encoding="utf-8")
    held = tmp_path / "held_out"
    held.mkdir()
    (held / "test_held.py").write_text(textwrap.dedent(HELD_OUT_TEST), encoding="utf-8")
    return skill, own, held


@pytest.fixture
def runner():
    return LocalIsolatedRunner()


def test_a_correct_skill_passes_both_suites(runner, tmp_path):
    skill, own, held = build(tmp_path)
    report = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)
    assert report.clean, report.as_dict()
    assert (report.passed, report.failed) == (1, 0)
    assert (report.held_out_passed, report.held_out_failed) == (1, 0)
    assert report.violations == []


def test_the_harness_catches_what_the_generators_own_tests_missed(runner, tmp_path):
    """This is the whole reason the held-out set exists.

    `BUFFER_BLIND` passes its own test and fails the owner's. If the report only ran the coder's
    tests, this skill would come back clean — a real defect in a routine that decides whether a
    private commitment gets overrun, waved through by the tests written by the thing that wrote it.
    """
    skill, own, held = build(tmp_path, skill_source=BUFFER_BLIND)
    report = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)
    assert (report.passed, report.failed) == (1, 0), "its own test should still pass"
    assert report.held_out_failed == 1, "the held-out case should catch it"
    assert report.clean is False


def test_a_run_with_no_held_out_cases_is_not_a_pass(runner, tmp_path):
    """An absent measurement must not read as a clean one. `clean` requires held-out evidence."""
    skill, own, _ = build(tmp_path)
    report = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=None)
    assert report.passed == 1
    assert report.clean is False
    assert any("not evidence" in note for note in report.notes)


def test_held_out_tests_inside_the_skill_directory_are_refused(runner, tmp_path):
    """The layout that makes the verdict meaningless, refused rather than warned about.

    A skill that can edit the tests grading it will eventually pass them, and the report would look
    exactly like a real pass.
    """
    skill, own, held = build(tmp_path)
    inside = skill / "held_out"
    inside.mkdir()
    (inside / "test_held.py").write_text(textwrap.dedent(HELD_OUT_TEST), encoding="utf-8")

    with pytest.raises(HeldOutTestsInsideSkill, match="inside the skill directory"):
        run_harness(runner, skill, MANIFEST, own_tests=own, held_out=inside)


def test_a_skill_that_goes_looking_for_the_held_out_tests_is_reported(runner, tmp_path):
    """A skill trying to pass by reading the answers is stopped, and the violation reaches the
    report rather than being swallowed as an ordinary failure."""
    skill, own, held = build(tmp_path, skill_source="""
        def run(inputs):
            import glob, os
            found = []
            for pattern in ("/tmp/**/test_held.py", "../**/test_held.py"):
                found += glob.glob(pattern, recursive=True)
            return {"ranked": found}
    """)
    report = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)
    assert not report.clean
    assert report.violations, "the escape attempt should surface as a named violation"


def test_the_identity_in_the_report_is_the_identity_that_was_graded(runner, tmp_path):
    """The report carries the hash the approval binds to, and a different skill yields a different
    one — so a report can never be presented against a skill it did not run."""
    skill, own, held = build(tmp_path)
    first = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)

    (skill / "rank_slots.py").write_text(textwrap.dedent(BUFFER_BLIND), encoding="utf-8")
    second = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)

    assert first.identity != second.identity
    assert first.identity.startswith("sha256:")


def test_the_identity_covers_the_held_out_tests_too(runner, tmp_path):
    skill, own, held = build(tmp_path)
    before = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held).identity
    (held / "test_held.py").write_text(
        textwrap.dedent(HELD_OUT_TEST) + "\n\ndef test_extra():\n    assert True\n",
        encoding="utf-8")
    after = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held).identity
    assert before != after, "relaxing the held-out tests must invalidate the identity"


def test_a_missing_skill_module_is_refused(runner, tmp_path):
    skill, own, held = build(tmp_path)
    (skill / "rank_slots.py").unlink()
    with pytest.raises(HarnessError, match="does not exist"):
        run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)


def test_a_syntax_error_in_a_test_module_is_reported_as_a_harness_error(runner, tmp_path):
    """Discovered with `ast`, so a broken test module is a harness error and not a mysteriously
    failing case."""
    skill, own, held = build(tmp_path)
    (held / "test_broken.py").write_text("def test_x(:\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="not valid Python"):
        run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)


def test_the_report_serialises_for_the_task_card(runner, tmp_path):
    skill, own, held = build(tmp_path, skill_source=BUFFER_BLIND)
    report = run_harness(runner, skill, MANIFEST, own_tests=own, held_out=held)
    payload = report_json(report)
    assert '"held_out_failed": 1' in payload
    assert '"clean": false' in payload
    assert "rank_slots" in payload
