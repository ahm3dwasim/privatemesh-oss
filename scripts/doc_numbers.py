#!/usr/bin/env python
"""Re-measure every count the README states, and keep the README's copy of them honest.

A number written into prose is a claim about a tree that no longer exists the moment anyone adds a
test. This script is the alternative: it computes each count from the tree as it is now and writes
them into a marked block in `README.md`. `--check` recomputes and fails if the block no longer
matches, which is what `tests/test_doc_consistency.py` runs.

    python scripts/doc_numbers.py            # print the numbers
    python scripts/doc_numbers.py --write    # regenerate the README block
    python scripts/doc_numbers.py --check    # exit 1 if the README block is stale

Every count is read from the same machinery the tests use, so the number in the doc cannot drift
from the thing it describes: the routes come from the router the gate test enumerates, and the
red-team count comes from the corpus loader the engine itself calls.
"""
from __future__ import annotations

import argparse
import functools
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = ROOT / "README.md"

BEGIN = "<!-- BEGIN GENERATED: doc-numbers"
END = "<!-- END GENERATED: doc-numbers -->"
BLOCK_RE = re.compile(
    r"<!-- BEGIN GENERATED: doc-numbers.*?-->.*?<!-- END GENERATED: doc-numbers -->",
    re.DOTALL,
)

#: The tests are not a package, and the route count has to be taken from the LIVE router — the
#: same enumeration `tests/test_fix_route_gate.py` performs — rather than from a count of
#: decorators, which would miss every route an `include_router` or a mount contributes.
TESTS = ROOT / "tests"

TEST_DEF_RE = re.compile(r"^[ \t]*def test_", re.M)
PARAMETRIZE_RE = re.compile(r"@pytest\.mark\.parametrize")


def count_tests() -> dict[str, int]:
    """Test functions, the files they live in, and how many of those files are parametrized."""
    files = sorted(p for p in TESTS.rglob("*.py") if p.is_file())
    functions = 0
    with_defs = 0
    parametrized = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        found = len(TEST_DEF_RE.findall(text))
        if found:
            with_defs += 1
            functions += found
        if PARAMETRIZE_RE.search(text):
            parametrized += 1
    return {"test_functions": functions, "test_files": with_defs,
            "parametrized_files": parametrized}


def count_redteam_cases() -> int:
    """The corpus the engine actually loads, not the number of files that look like cases."""
    sys.path.insert(0, str(ROOT / "src"))
    from privatemesh.redteam import CASES_DIR, load_cases

    return len(load_cases(CASES_DIR))


@functools.lru_cache(maxsize=1)
def _mutcheck():
    """`redteam/mutcheck_findings.py` as a module, loaded rather than grepped.

    Cached because `module_from_spec` does not put the module in `sys.modules`, so a second call
    would re-execute the file — two thousand lines of control definitions, twice, to read two
    lengths off it."""
    import importlib.util

    path = ROOT / "redteam" / "mutcheck_findings.py"
    spec = importlib.util.spec_from_file_location("_mutcheck_counts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def count_mutation_controls() -> int:
    """The controls a PLAIN run covers — `MUTATIONS`, the default list.

    Which set this counts is the point of there being two rows: a reader who runs the command in
    the third column runs this many, and `--all` runs a different, larger number. The README stated
    one of them without saying which, which is a number a reader cannot act on.
    """
    return len(_mutcheck().MUTATIONS)


def count_all_mutation_controls() -> int:
    """The controls `--all` covers, read from the runner's own `ALL_MUTATIONS`.

    Not re-concatenated here. The README would then be measuring an expression of its own rather
    than the one the flag runs, and the two part company the first time a list is added to one of
    them.
    """
    return len(_mutcheck().ALL_MUTATIONS)


def count_v2_routes() -> int:
    """`(METHOD, path)` pairs on the v2 router, enumerated the way the gate test enumerates them."""
    sys.path.insert(0, str(TESTS))
    from test_fix_route_gate import _live_router, _routes

    _h, _service, _api, router = _live_router()
    return len(_routes(router))


def collect() -> dict[str, int]:
    numbers = {}
    numbers.update(count_tests())
    numbers["redteam_cases"] = count_redteam_cases()
    numbers["mutation_controls"] = count_mutation_controls()
    numbers["all_mutation_controls"] = count_all_mutation_controls()
    numbers["v2_routes"] = count_v2_routes()
    return numbers


#: Order and wording of the generated table. Each row names the command a reader can run to
#: disagree with us, because a number a reader cannot reproduce is a number they have to trust.
ROWS = [
    ("test_functions", "test functions in `tests/`",
     r'grep -rhE "^[[:space:]]*def test_" tests | wc -l'),
    ("test_files", "test files containing them", "—"),
    ("parametrized_files", "of those files, parametrized", "—"),
    ("redteam_cases", "injection red-team cases",
     r'python -c "from privatemesh.redteam import load_cases, CASES_DIR; '
     r'print(len(load_cases(CASES_DIR)))"'),
    ("mutation_controls", "mutation controls in the DEFAULT run of `redteam/mutcheck_findings.py`",
     r'python redteam/mutcheck_findings.py'),
    ("all_mutation_controls", "mutation controls in ALL of `redteam/mutcheck_findings.py` "
                              "(`--all`)",
     r'python redteam/mutcheck_findings.py --all'),
    ("v2_routes", "routes on the v2 router",
     "python scripts/doc_numbers.py"),
]


def render(numbers: dict[str, int]) -> str:
    lines = [
        f"{BEGIN} — regenerate with `python scripts/doc_numbers.py --write` -->",
        "",
        "| Count | Value | Re-measure with |",
        "| --- | --- | --- |",
    ]
    for key, label, command in ROWS:
        shown = f"`{command}`" if command != "—" else "—"
        lines.append(f"| {label} | **{numbers[key]}** | {shown} |")
    lines += ["", END]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite the README block")
    parser.add_argument("--check", action="store_true", help="fail if the README block is stale")
    args = parser.parse_args(argv)

    numbers = collect()
    block = render(numbers)

    for key, label, _command in ROWS:
        print(f"{numbers[key]:>6}  {label}")

    if args.write:
        text = README.read_text(encoding="utf-8")
        if not BLOCK_RE.search(text):
            print(f"no generated block in {README}; add {BEGIN} ... {END}", file=sys.stderr)
            return 2
        README.write_text(BLOCK_RE.sub(lambda _m: block, text, count=1), encoding="utf-8")
        print(f"\nwrote {README}")
        return 0

    if args.check:
        text = README.read_text(encoding="utf-8")
        found = BLOCK_RE.search(text)
        if not found:
            print(f"{README}: no generated block found", file=sys.stderr)
            return 1
        if found.group(0) != block:
            print(f"\n{README}: the generated block is stale. Re-run with --write.",
                  file=sys.stderr)
            print("--- in the file ---", file=sys.stderr)
            print(found.group(0), file=sys.stderr)
            print("--- measured now ---", file=sys.stderr)
            print(block, file=sys.stderr)
            return 1
        print(f"\n{README}: the generated block matches the tree.")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
