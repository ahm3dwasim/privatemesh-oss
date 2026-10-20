"""The held-out file is not hand-written, and this is the test that keeps it that way.

Every expectation in `tests/heldout/meeting_option_evaluator/heldout_meeting_options.py` was
DERIVED from `learn.reference_run`. That is a good property with a bad failure mode: change
`fit.py` and the derived file silently becomes a stale answer key, grading generated code against a
rule the product no longer follows — and it would keep passing, which is the worst thing a test can
do.

So this module re-derives all twenty cases from the current `reference_run` and compares. If the
trusted validator moves, the fix is to regenerate the held-out file, and this failure is what says
so.

It also pins the two properties that make the arrangement worth anything:
`run_harness` refuses held-out tests inside the skill directory, and `reference_run` applies the
validator's rules BEFORE the two rules the validator does not own.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from privatemesh.learn import RULE_ORDER, agrees_with_reference, reference_run
from privatemesh.skillrt import HeldOutTestsInsideSkill
from privatemesh.skillrt.harness import HarnessError, _assert_separate

HELD_OUT_DIR = Path(__file__).resolve().parent / "heldout" / "meeting_option_evaluator"
HELD_OUT_FILE = HELD_OUT_DIR / "heldout_meeting_options.py"


def _declared_cases() -> dict[str, dict]:
    """Read the case list WITHOUT importing the module.

    Importing it would execute `import meeting_option_evaluator` in this process, which is exactly
    what the whole package exists to avoid: the file under test is generated code, and the trusted
    process does not run it. `ast` gives the literal, which is what `skillrt.harness` does too.
    """
    tree = ast.parse(HELD_OUT_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_CASES_JSON" for t in node.targets):
            assert isinstance(node.value, ast.Constant), "_CASES_JSON must be a literal"
            return {case["name"]: case for case in json.loads(node.value.value)}
    raise AssertionError("the held-out file no longer defines _CASES_JSON")


CASES = _declared_cases()


def test_the_held_out_file_lives_outside_any_skill_directory():
    """The structural property, stated first because everything else here is worthless without it:
    tests the code being graded can edit are not held out."""
    assert HELD_OUT_DIR.is_dir()
    assert not any(p.name == "meeting_option_evaluator.py" for p in HELD_OUT_DIR.iterdir()), (
        "the held-out directory must not contain the module it grades")
    # And the harness REFUSES the arrangement rather than trusting this assert — the negative
    # control, without which the check above would be a comment.
    with pytest.raises(HeldOutTestsInsideSkill):
        _assert_separate(Path("/tmp/pmskill/skill"), Path("/tmp/pmskill/skill/heldout"))
    # The other direction is refused too, as a `HarnessError`: a skill directory inside the
    # held-out tree is the layout backwards, and grading it would prove nothing either.
    with pytest.raises(HarnessError, match="layout is backwards"):
        _assert_separate(Path("/tmp/pmskill/skill"), Path("/tmp/pmskill"))


def test_there_are_twenty_cases_and_every_one_has_a_test_function():
    """A case with no `test_` function is a case that never runs, and a run with zero held-out
    cases is not a pass — it is an absent measurement."""
    source = HELD_OUT_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {n.name for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}
    assert len(CASES) == 20
    assert len(functions) == len(CASES), (
        f"{len(CASES)} cases but {len(functions)} test functions")
    for name in CASES:
        assert f"test_{name}" in functions, f"case {name!r} has no test function"


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_expected_option_decision_is_what_the_reference_says_today(name):
    """The one assertion this file exists for. If `fit.py`'s rules move, this fails and the
    held-out file is regenerated — rather than the skill being graded against a stale answer."""
    case = CASES[name]
    expected = reference_run(case["inputs"])
    assert case["options"] == [{"fits": o["fits"], "code": o["code"]}
                               for o in expected["options"]], (
        f"{name}: the held-out answer key no longer matches `reference_run`. Regenerate "
        f"`heldout_meeting_options.py` if the validator change is intended.")


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_expected_ranking_and_venue_is_what_the_reference_says_today(name):
    case = CASES[name]
    expected = reference_run(case["inputs"])
    assert case["ranked"] == [r["start"] for r in expected["ranked"]]
    assert case["venues"] == [r["venue"]["name"] if r["venue"] else None
                              for r in expected["ranked"]]


def test_the_reference_agrees_with_itself_so_the_cross_check_can_be_exact():
    """The live flow compares a learned skill against `reference_run` on the same inputs. That
    comparison is only meaningful if `reference_run` is deterministic and if a result fed back in
    reports no disagreement — otherwise every plan would be blocked by a phantom mismatch."""
    for name, case in CASES.items():
        assert agrees_with_reference(case["inputs"], reference_run(case["inputs"])) == [], name


def test_a_widened_answer_is_reported_by_the_cross_check():
    """The negative control. Without it, `agrees_with_reference` returning `[]` everywhere would
    look like agreement rather than like a function that never disagrees."""
    case = CASES["nothing_fits_at_all"]
    widened = reference_run(case["inputs"])
    widened["options"][0]["fits"] = True
    problems = agrees_with_reference(case["inputs"], widened)
    assert problems and "fits=True" in problems[0]


def test_a_dropped_option_is_reported_rather_than_treated_as_a_narrowing():
    """A skill that returns fewer options than slots was never asked the question. Treating that
    as "it narrowed them away" would let a broken skill silently empty a plan."""
    case = CASES["only_the_slots_that_fit_are_ranked"]
    shortened = reference_run(case["inputs"])
    shortened["options"] = shortened["options"][:-1]
    problems = agrees_with_reference(case["inputs"], shortened)
    assert problems and "options for" in problems[0]


def test_the_published_rule_order_puts_the_validators_rules_first():
    """The one place this capability is a superset of `fit.py`, made explicit. `wrong_duration`
    and `too_soon` come AFTER every rule the validator owns, because the validator decides first
    and a skill that reordered them would disagree with it on the cases the validator owns."""
    validator_rules = ("empty_slot", "in_the_past", "outside_day", "overlaps_protected_buffer",
                       "overlaps_protected", "overlaps_busy")
    assert RULE_ORDER[-1] == "fits"
    assert RULE_ORDER.index("wrong_duration") > max(RULE_ORDER.index(r) for r in validator_rules)
    assert RULE_ORDER.index("too_soon") > max(RULE_ORDER.index(r) for r in validator_rules)


def test_the_zero_means_no_constraint_rule_is_load_bearing():
    """`duration_minutes=0` and `minimum_notice_minutes=0` mean "the caller states no constraint",
    not "zero minutes of it". Read literally, a zero-minute duration would refuse every slot, and
    the live flow — which passes 0 for both — would block every plan it ever made."""
    inputs = {"offered_slots": [{"start": "2026-09-19T15:00:00+01:00",
                                 "end": "2026-09-19T15:45:00+01:00"}],
              "duration_minutes": 0, "minimum_notice_minutes": 0}
    assert reference_run(inputs)["options"][0]["fits"] is True
    assert reference_run({**inputs, "duration_minutes": 45})["options"][0]["fits"] is True
    assert reference_run({**inputs, "duration_minutes": 30})["options"][0]["fits"] is False
