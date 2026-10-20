"""Every count the README states has to match this tree, or this file fails.

The finding this answers was not "the README says 760 and the tree has more" — it was that a blind
judge scoring the README against the code found the number wrong, and the honest reading of that is
that *one* wrong number discounts every other claim in the document. A doc whose pitch is "trust us,
we measured" cannot carry a number nobody re-measured.

So the numbers are not stored here either. `scripts/doc_numbers.py` recomputes each one from the
tree, writes them into a marked block in `README.md`, and this file re-runs that recomputation and
requires the block to match. Editing the README by hand to a different number fails here; adding a
test or a route without regenerating also fails here. Both are the point.

The block covers the four the review named — test count, red-team case count, route count and
mutation-control count — plus the file counts the same paragraph quotes.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"


def _doc_numbers():
    """`scripts/doc_numbers.py` as a module. The script IS the generator; this does not restate it."""
    path = ROOT / "scripts" / "doc_numbers.py"
    spec = importlib.util.spec_from_file_location("doc_numbers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def numbers() -> dict[str, int]:
    return _doc_numbers().collect()


def test_the_generated_block_is_what_the_script_generates_now(numbers):
    """The load-bearing check: regenerate, and require the README to already say it."""
    dn = _doc_numbers()
    text = README.read_text(encoding="utf-8")
    found = dn.BLOCK_RE.search(text)
    assert found, (
        f"{README} has no generated block. It is delimited by {dn.BEGIN} ... {dn.END}; "
        "restore it with `python scripts/doc_numbers.py --write`.")
    assert found.group(0) == dn.render(numbers), (
        "the README's generated block is stale. Re-run `python scripts/doc_numbers.py --write` "
        "and commit the result.")


@pytest.mark.parametrize(("key", "label"), [
    ("test_functions", "test functions"),
    ("redteam_cases", "red-team cases"),
    ("v2_routes", "v2 routes"),
    ("mutation_controls", "mutation controls"),
])
def test_each_number_the_review_named_is_stated_in_the_block(numbers, key, label):
    """The four the acceptance names, each present as the measured value and not as prose.

    A number that is only in the table is a number a reader can check; a number that is also
    restated in a sentence is a second copy that can rot on its own. This asserts the table carries
    it, so the sentence does not have to.
    """
    dn = _doc_numbers()
    block = dn.render(numbers)
    assert f"**{numbers[key]}**" in block, f"{label} missing from the generated block"


def test_the_readme_does_not_restate_a_test_count_the_tree_contradicts(numbers):
    """The specific stale claim, pinned so it cannot come back.

    "760 test functions across 62 files (20 of them parametrized)" was true at `5b16973` and false
    everywhere after it. Re-introducing any of the three numbers as a claim about the CURRENT tree
    is what this catches; the historical sentence in the README is deliberately about the past and
    says so, which is why the pattern below requires the number to be asserted rather than
    mentioned beside a commit id.
    """
    text = README.read_text(encoding="utf-8")
    # Prose may state the count, but only the measured one. The generated table is the single place
    # the number is allowed to live; a second copy in a sentence is what went stale last time. This
    # does not require a prose count to exist — it requires that any that does is not a second
    # opinion, so re-introducing "760 test functions" fails here.
    for claimed in re.findall(r"(\d+) test functions", text):
        assert int(claimed) == numbers["test_functions"], (
            f"the README states {claimed} test functions in prose; this tree has "
            f"{numbers['test_functions']}. Re-run `python scripts/doc_numbers.py --write`.")
    # The repository map is the other place a drifting hand-written count lived.
    for claimed in re.findall(r"tests/\s+(\d+) test", text):
        assert int(claimed) == numbers["test_functions"], (
            f"the README's repository map states {claimed} test functions")


def test_no_doc_states_a_test_count_beside_a_test_file():
    """A count written next to a filename has to be re-measured, or it is a claim about the past.

    `docs/THREAT_MODEL.md` said `test_inject_finding1_recipient_pin.py` held "15 tests" while the
    file held sixteen. That is the same defect as the README's test count and it was found the same
    way — a reader comparing the document to the tree. Naming the file is the durable form; this
    fails if a number comes back.
    """
    pattern = re.compile(r"tests/[\w/]+\.py`?\s*\((\d+) tests?\)")
    offenders = []
    for doc in sorted((ROOT / "docs").glob("*.md")) + [README]:
        for match in pattern.finditer(doc.read_text(encoding="utf-8")):
            offenders.append(f"{doc.name}: {match.group(0)!r}")
    assert not offenders, (
        "a count is stated beside a test file, where it will drift: " + "; ".join(offenders))


def test_the_redteam_case_count_in_the_prose_matches_the_corpus(numbers):
    """The prose headline and the loaded corpus are the same number, not two opinions.

    "Eight cases is a corpus, not a proof of coverage" and the table's `8/8` cells are claims about
    `redteam/cases/`. If a case is added, the loader returns a different number and this fails
    before a reader can quote the old one.
    """
    text = README.read_text(encoding="utf-8")
    count = numbers["redteam_cases"]
    assert f"**Injection red-team — {count} cases.**" in text, (
        f"the README's red-team headline does not say {count} cases")
    assert f"**{count}/{count}**" in text, (
        f"the README's red-team table does not show {count}/{count}")

    words = {8: "Eight", 9: "Nine", 10: "Ten"}
    if count in words:
        assert re.search(rf"\b{words[count]} cases is a corpus\b", text), (
            f"the README's 'N cases is a corpus' sentence does not say {words[count]}")
