"""Grading a generated skill: its own tests, and tests it has never seen.

A generator that writes a skill and writes the tests for that skill has proved nothing. It passes
the tests it wrote to pass. The evidence that a learned capability is real has to come from cases
the coder never saw, and — this is the part that is easy to get wrong — those cases must be
unreachable by the code being graded. `HeldOutTestsInsideSkill` below refuses the arrangement where
they are not.

Where the held-out tests live is the security property, so it is enforced structurally:

  * the held-out directory may not be inside the skill's directory (checked before anything runs);
  * inside the child, that directory is on the allow-list and on `sys.path` ONLY while the test
    module is being imported, and both are revoked before the test function is called — so the
    moment the skill's own code starts running, the tests grading it are unreadable.

This module never imports a test module itself. It reads them with `ast` instead: a test module
imports the skill, and importing it here would execute generated code in the trusted process. That
is the exact thing this whole package exists to avoid.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import SkillManifest
from .runner import OK, RunResult, Runner, RunSpec
from .violations import Violation

OWN, HELD_OUT = "own", "held_out"


class HarnessError(RuntimeError):
    """The harness was set up in a way that makes its verdict meaningless."""


class HeldOutTestsInsideSkill(HarnessError):
    """The held-out tests are inside the skill's own directory.

    Refused rather than warned about, because the failure is silent otherwise: everything passes,
    and the report claims evidence it does not have. Tests the coder can edit are not held out.
    """


@dataclass(frozen=True)
class CaseResult:
    suite: str          # own | held_out
    name: str           # module::test_function
    passed: bool
    result: RunResult

    @property
    def violation(self) -> Violation | None:
        return self.result.violation


@dataclass(frozen=True)
class HarnessReport:
    """The verdict, in the shape the task card shows: how many of the coder's own cases passed, how
    many of the owner's unseen cases passed, and every violation the runtime named on the way."""

    skill: str
    identity: str
    passed: int = 0
    failed: int = 0
    held_out_passed: int = 0
    held_out_failed: int = 0
    violations: list[Violation] = field(default_factory=list)
    cases: list[CaseResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.held_out_passed + self.held_out_failed

    @property
    def clean(self) -> bool:
        """No failures anywhere, no violations, and at least one held-out case actually ran. A run
        with zero held-out cases is not a pass — it is an absent measurement."""
        return (self.failed == 0 and self.held_out_failed == 0 and not self.violations
                and self.held_out_passed > 0)

    def as_dict(self) -> dict:
        return {"skill": self.skill, "identity": self.identity, "passed": self.passed,
                "failed": self.failed, "held_out_passed": self.held_out_passed,
                "held_out_failed": self.held_out_failed, "clean": self.clean,
                "violations": [str(v) for v in self.violations], "notes": self.notes,
                "cases": [{"suite": c.suite, "name": c.name, "passed": c.passed,
                           "status": c.result.status, "detail": c.result.error or "",
                           "violation": str(c.violation) if c.violation else None}
                          for c in self.cases]}


def _discover(directory: Path) -> list[tuple[str, str, Path]]:
    """(module name, test function, path) for every top-level `test_*` in `directory/*.py`.

    Parsed with `ast`, never imported — see the module docstring."""
    found: list[tuple[str, str, Path]] = []
    for path in sorted(Path(directory).glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            raise HarnessError(f"{path} is not valid Python: {exc}") from exc
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name.startswith("test_") and not node.args.args:
                found.append((path.stem, node.name, path))
    return found


def _source(directory: Path) -> str:
    """Every `.py` under `directory`, concatenated. Used only to hash the identity."""
    if not Path(directory).exists():
        return ""
    return "\n".join(sorted(p.read_text(encoding="utf-8", errors="replace")
                            for p in Path(directory).rglob("*.py")))


def source_of(directory: Path) -> str:
    """The public name for `_source`: the bytes that go into an identity hash.

    It is public because a caller that PERSISTS an identity has to be able to re-derive it later
    from what it stored — otherwise "this approval still binds to this artifact" is a claim the
    library makes about a hash it cannot recompute."""
    return _source(directory)


def _assert_separate(skill_dir: Path, held_out_dir: Path) -> None:
    skill_dir, held_out_dir = Path(skill_dir).resolve(), Path(held_out_dir).resolve()
    if held_out_dir == skill_dir or skill_dir in held_out_dir.parents:
        raise HeldOutTestsInsideSkill(
            f"held-out tests at {held_out_dir} are inside the skill directory {skill_dir}; "
            f"the skill could rewrite them, so their verdict would mean nothing")
    if held_out_dir in skill_dir.parents:
        raise HarnessError(f"the skill directory {skill_dir} is inside the held-out tests "
                           f"{held_out_dir}; the layout is backwards")


def run_harness(runner: Runner, skill_dir: Path, manifest: SkillManifest, *,
                own_tests: Path | None = None, held_out: Path | None = None,
                read_roots_extra: tuple[Path, ...] = ()) -> HarnessReport:
    """Run the skill's own tests and the held-out set, and report both.

    `own_tests` is what the generator wrote; `held_out` is what the owner wrote and the coder never
    saw. They are graded identically, by the same runner, so a difference between the two columns
    is a difference in the cases and not in the rigour.
    """
    skill_dir = Path(skill_dir).resolve()
    module_path = skill_dir / f"{manifest.module_name}.py"
    if not module_path.is_file():
        raise HarnessError(f"skill '{manifest.name}' declares module "
                           f"{manifest.module_name!r} but {module_path} does not exist")
    if held_out is not None:
        _assert_separate(skill_dir, Path(held_out))

    identity = manifest.identity(
        module_path.read_text(encoding="utf-8"),
        _source(own_tests) if own_tests else "",
        held_out=_source(held_out) if held_out else "")

    notes: list[str] = []
    cases: list[CaseResult] = []

    for suite, directory in ((OWN, own_tests), (HELD_OUT, held_out)):
        if directory is None:
            if suite is HELD_OUT:
                notes.append("no held-out tests were supplied — this run is not evidence "
                             "that the skill generalises")
            continue
        extra = (Path(directory).resolve(),) + tuple(read_roots_extra)
        for module_name, test_name, path in _discover(Path(directory)):
            # One process per case: a skill that hangs, crashes the interpreter, or trips a
            # violation must not be able to take the rest of the suite down with it.
            result = runner.run(RunSpec(skill_dir=skill_dir, module=module_name, entry=test_name,
                                        pass_inputs=False, read_roots=extra))
            cases.append(CaseResult(suite, f"{module_name}::{test_name}",
                                    result.status == OK, result))

    report = HarnessReport(
        skill=manifest.name, identity=identity,
        passed=sum(1 for c in cases if c.suite == OWN and c.passed),
        failed=sum(1 for c in cases if c.suite == OWN and not c.passed),
        held_out_passed=sum(1 for c in cases if c.suite == HELD_OUT and c.passed),
        held_out_failed=sum(1 for c in cases if c.suite == HELD_OUT and not c.passed),
        violations=[c.violation for c in cases if c.violation],
        cases=cases, notes=notes)
    return report


def report_json(report: HarnessReport) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True)
