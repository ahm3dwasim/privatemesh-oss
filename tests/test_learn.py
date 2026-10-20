"""The learning loop, driven the way the API drives it.

Three claims, and each is the reason one of the pieces exists:

* **v1 overreaches and is STOPPED.** The generator is handed the manifest it must fill in and the
  rules in order; it answers with code that imports `urllib`. The isolated runtime refuses it by
  name before the entry point exists, both test suites fail with that violation attached, and the
  reviewer — a different model family — rejects it too. The attempt is not quietly kept.
* **v2 computes and is READY.** It passes the tests it wrote and the twenty cases it never saw.
* **Nothing about the held-out set reaches the generator.** Asserted on the CAPTURED prompts, not
  on an intention: the recorder is non-empty and no captured payload contains a held-out case name
  or its expected answer.

The scripted generator/reviewer is reached through `wiring.sample_reply` — the production script,
routed on the prompt's own content — so what is exercised is the real prompt, the real tier router
and the real spend ledger, not a test double's idea of them.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from privatemesh import wiring as wm
from privatemesh.config import Settings
from privatemesh.learn import (CAPABILITY, GENERATE_SYSTEM, REVIEW_SYSTEM, ApprovalNotReady,
                               ApprovalStale, Learner)
from privatemesh.library import ACTIVE, SkillLibrary
from privatemesh.llm import TokenFactory
from privatemesh.sample_skill_code import V1_MANIFEST, V2_MANIFEST
from privatemesh.skillrt import HeldOutTestsInsideSkill  # noqa: F401  (re-exported for readers)
from privatemesh.spend import PRICES, SpendLedger

HELD_OUT_DIR = Path(__file__).resolve().parent / "heldout" / CAPABILITY

#: The REAL model ids, not sample placeholders, because this file runs the spend ledger for real:
#: `SpendLedger.reserve` refuses a model with no known price, and a made-up tier name would make the
#: "both models are charged" assertion pass for the wrong reason.
SETTINGS = Settings(api_key="sample-no-key", base_url="https://tf.test/v1/",
                    model_fast="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                    model_reason="nvidia/nemotron-3-super-120b-a12b",
                    model_deep="nvidia/Nemotron-3-Ultra-550b-a55b", tavily_key="")


class Recorder:
    """Every request the loop made, verbatim. The prompt-absence assertions read THIS — "the
    held-out cases never reached the model" is worth nothing unless something was captured."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def add(self, body: dict) -> None:
        self.requests.append(body)

    def systems(self) -> list[str]:
        return [next((m["content"] for m in r.get("messages") or [] if m["role"] == "system"), "")
                for r in self.requests]

    def blob(self) -> str:
        return json.dumps(self.requests, ensure_ascii=False)


#: A three-case held-out set, for the tests that are about the LOOP rather than about the grading.
#: Each case costs a subprocess — the runtime starts one per case, deliberately, so a skill that
#: hangs or trips a violation cannot take the suite down with it — and twenty cases times two
#: attempts is 54 processes to prove something three cases prove just as well. The full set is
#: graded once, in `test_the_real_held_out_set_grades_the_revision_twenty_for_twenty`.
SMALL_CASES = ("a_slot_that_has_already_ended_is_in_the_past",
               "a_slot_of_the_wrong_length_is_refused",
               "the_first_official_source_is_the_venue")


def _small_held_out(directory: Path) -> Path:
    """Write a real held-out module, derived from `reference_run`, the same way the owner's is."""
    from privatemesh.learn import reference_run

    cases = {name: case for name, case in _real_cases().items() if name in SMALL_CASES}
    assert len(cases) == len(SMALL_CASES)
    out = directory / "heldout"
    out.mkdir(parents=True)
    (out / "heldout_small.py").write_text(
        "import json\n\n"
        "import meeting_option_evaluator\n\n"
        f"_CASES = json.loads(r'''{json.dumps(cases)}''')\n\n\n"
        "def _check(name):\n"
        "    case = _CASES[name]\n"
        "    result = meeting_option_evaluator.run(case['inputs'])\n"
        "    assert [{'fits': o['fits'], 'code': o['code']} for o in result['options']] == "
        "case['options']\n"
        "    assert [r['start'] for r in result['ranked']] == case['ranked']\n\n\n"
        + "".join(f"def test_{name}():\n    _check({name!r})\n\n\n" for name in cases),
        encoding="utf-8")
    return out


def _real_cases() -> dict[str, dict]:
    """The owner's actual held-out cases, read without importing the module (see
    `test_learn_reference`)."""
    import ast

    tree = ast.parse((HELD_OUT_DIR / "heldout_meeting_options.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_CASES_JSON" for t in node.targets):
            return {c["name"]: c for c in json.loads(node.value.value)}
    raise AssertionError("the held-out file no longer defines _CASES_JSON")


def _learner(held_out: Path, *, library: SkillLibrary | None = None):
    """A Learner over a recording transport that answers from `wiring.sample_reply` — the
    production script, reached the way production reaches it."""
    recorder = Recorder()
    spend = SpendLedger()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        recorder.add(body)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": wm.sample_reply(body)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 400, "total_tokens": 1300}})

    http = httpx.Client(base_url=SETTINGS.base_url, timeout=5,
                        transport=httpx.MockTransport(handler))
    library = library if library is not None else SkillLibrary()
    learner = Learner(llm=TokenFactory(SETTINGS, http, spend=spend), library=library,
                      held_out=held_out)
    return learner, library, recorder, spend, held_out


@pytest.fixture
def recorded(tmp_path):
    """The loop over a small held-out set: fast, and enough for everything that is not about
    grading."""
    learner, library, recorder, spend, held_out = _learner(_small_held_out(tmp_path))
    return learner, library, recorder, spend, held_out


def _run(learner: Learner, **kw):
    learning = learner.start(change_id=kw.pop("change_id", "chg-1"), **kw)
    import time
    for _ in range(4000):
        if learning.stage in ("ready_for_review", "failed"):
            return learning
        time.sleep(0.02)
    raise AssertionError(f"the loop never settled; stage={learning.stage}")


# ---------------------------------------------------------------- the loop ---------------------
def test_the_canned_v1_is_stopped_and_v2_is_ready(recorded):
    """The demo's claim, end to end: a first attempt that reaches for the network is refused, a
    revision that computes is approved. Both attempts stay on the record — the failed one is the
    evidence that the gate is real."""
    learner, library, _recorder, _spend, _held = recorded
    learning = _run(learner)
    assert learning.stage == "ready_for_review", learning.error
    assert len(learning.attempts) == 2, "the loop must show the attempt it refused"

    first, second = learning.attempts
    assert first["ok"] is False
    assert first["review"]["verdict"] == "reject"
    assert any(v["kind"] == "import" and "urllib" in v["detail"] for v in first["violations"])
    assert first["tests"] == {"passed": 0, "failed": 2}
    assert first["held_out"] == {"passed": 0, "failed": len(SMALL_CASES)}
    assert library.serves(CAPABILITY) is False, "a rejected attempt installs nothing"

    assert second["ok"] is True
    assert second["tests"] == {"passed": 7, "failed": 0}
    assert second["held_out"] == {"passed": len(SMALL_CASES), "failed": 0}
    assert second["violations"] == []
    assert second["review"]["verdict"] == "approve"


def test_v1_is_rejected_for_the_right_reason_and_not_merely_for_failing(recorded):
    """A reviewer that rejects everything would pass the test above. Its reasons must NAME the
    escape, and the report must be what told it — the same evidence the owner would see."""
    learner, _library, _recorder, _spend, _held = recorded
    first = _run(learner).attempts[0]
    reasons = " ".join(first["review"]["reasons"])
    assert "urllib" in reasons
    assert "import" in reasons, "the reviewer must cite the runtime's own violation, not a hunch"


def test_the_identity_under_review_is_the_one_the_harness_computed(recorded):
    learner, _library, _recorder, _spend, _held = recorded
    learning = _run(learner)
    assert learning.ready_identity.startswith("sha256:")
    assert learning.ready_identity == learning.attempts[-1]["identity"]
    assert learning.manifest["identity"] == learning.ready_identity


def test_the_reviewer_is_a_different_model_family_from_the_generator(recorded):
    """The claim is "a second model reviewed it". Same weights asked twice is not a second model,
    so the reviewer is requested by model id rather than through the tier table — and the ids that
    actually left the process are what is asserted."""
    learner, _library, recorder, _spend, _held = recorded
    _run(learner)
    systems = recorder.systems()
    generator_ids = [r["model"] for r, s in zip(recorder.requests, systems)
                     if s.startswith(GENERATE_SYSTEM)]
    reviewer_ids = [r["model"] for r, s in zip(recorder.requests, systems)
                    if s.startswith(REVIEW_SYSTEM)]
    assert generator_ids and reviewer_ids
    assert set(reviewer_ids) == {"Qwen/Qwen3-235B-A22B-Instruct-2507"}, (
        "every review call must go to the reviewer model id, not through the tier table")
    assert len(reviewer_ids) == len(generator_ids), (
        "the loop must review every attempt it generated")
    assert not set(generator_ids) & set(reviewer_ids), (
        "the reviewer must not be one of the generator's own tiers")
    assert reviewer_ids[0] in PRICES, "an unpriced model would be refused by the spend ledger"


def test_both_models_are_charged_and_the_ledger_recognises_both(recorded):
    """A second model family on the same endpoint is a second line on the bill. `settle` refuses an
    unknown model, so a reviewer that was not priced would fail here rather than overspend
    silently."""
    learner, _library, _recorder, spend, _held = recorded
    _run(learner)
    rows = spend._conn.execute("SELECT model, SUM(usd), state FROM spend GROUP BY model").fetchall()
    by_model = {r[0]: (r[1], r[2]) for r in rows}
    assert set(by_model) == {"nvidia/Nemotron-3-Ultra-550b-a55b",
                             "Qwen/Qwen3-235B-A22B-Instruct-2507"}, (
        f"both models must appear on the ledger, got {sorted(by_model)}")
    for model, (usd, state) in by_model.items():
        assert usd > 0 and state != "reserved", f"{model} was never settled"


# ---------------------------------------------------------------- the held-out boundary --------
def test_no_held_out_case_ever_reaches_the_generator_or_the_reviewer(recorded):
    """The security property, asserted on captured payloads.

    The single most important assertion in this file is that the recorder is NOT empty: "the
    held-out cases were absent" is worth nothing if no prompt was captured at all.
    """
    learner, _library, recorder, _spend, _held = recorded
    _run(learner)
    assert len(recorder.requests) >= 4, "nothing was captured, so absence proves nothing"

    blob = recorder.blob()
    # Run against the real held-out set too: the small one is a stand-in for speed, and the
    # assertion that matters is about the cases the OWNER wrote.
    for name in _real_cases():
        assert name not in blob, f"held-out case {name!r} reached a model"
    for name in SMALL_CASES:
        assert name not in blob, f"held-out case {name!r} reached a model"
    # ...and neither did any line distinctive to the held-out file. `import
    # meeting_option_evaluator` is deliberately NOT in this list: the coder's own tests import the
    # module by name, so it appears legitimately and would make this assertion fail on a prompt
    # that carried nothing secret.
    for line in ("_CASES_JSON", "def _check(", "heldout_meeting_options", "the_canonical_1600",
                 "a_slot_that_ends_exactly_at_the_buffer_start_still_fits"):
        assert line not in blob, f"{line!r} from the held-out file reached a model"


def test_the_generator_is_told_the_rules_in_order_but_never_the_answers(recorded):
    """What the generator DOES get: the contract it must implement, stated in full. What it does
    not get is a single expected output."""
    learner, _library, recorder, _spend, _held = recorded
    _run(learner)
    first = next(r for r, s in zip(recorder.requests, recorder.systems())
                 if s.startswith(GENERATE_SYSTEM))
    payload = json.loads(first["messages"][-1]["content"])
    assert payload["inputs_schema"]["required"] == ["offered_slots"]
    assert payload["rules_in_order"][-1] == "fits"
    assert set(payload["inputs_schema"]["properties"]) == set(V1_MANIFEST["reads"])
    assert "held_out" not in first["messages"][-1]["content"]


# ---------------------------------------------------------------- approval ---------------------
def test_approving_the_wrong_identity_installs_nothing(recorded):
    """An approval binds to a hash. Anything else is a 409, and the artifact stays out of the
    library — the failure mode this prevents is "approved" meaning "the screen I happened to be
    looking at"."""
    learner, library, _recorder, _spend, _held = recorded
    learning = _run(learner)
    with pytest.raises(ApprovalStale):
        learner.approve(learning.id, "sha256:" + "0" * 64)
    with pytest.raises(ApprovalStale):
        learner.approve(learning.id, "")
    assert library.serves(CAPABILITY) is False


def test_approving_the_identity_on_the_screen_installs_exactly_those_bytes(recorded):
    learner, library, _recorder, _spend, held = recorded
    learning = _run(learner)
    row = learner.approve(learning.id, learning.ready_identity)
    assert row["identity"] == learning.ready_identity
    assert row["state"] == ACTIVE
    assert row["purpose_plain"] == learning.purpose_plain
    manifest, code, _tests, held_out, _row = library.load(CAPABILITY)
    assert code == learning.artifact["code"]
    assert manifest.version == "1.0.1", (
        "the approved version is the revision, not the first attempt")
    stored = "\n".join(p.read_text(encoding="utf-8") for p in sorted(held.iterdir()))
    assert held_out == stored, (
        "the held-out bytes are part of the identity, so they are stored with it — otherwise "
        "changing what a skill is graded on would not change what the owner approved")
    assert row["identity"] == manifest.identity(code, _tests, held_out=held_out)


def test_approving_before_the_loop_finishes_is_refused(recorded):
    """The UI polls until `ready_for_review`. A client that does not must be told, not served."""
    learner, library, _recorder, _spend, _held = recorded
    learning = learner._new("chg-1", CAPABILITY, "…")
    with pytest.raises(ApprovalNotReady):
        learner.approve(learning.id, "sha256:whatever")
    assert library.serves(CAPABILITY) is False


def test_rejecting_leaves_the_library_empty_and_the_record_honest(recorded):
    learner, library, _recorder, _spend, _held = recorded
    learning = _run(learner)
    ready = learning.ready_identity
    assert ready, "there was nothing to reject"
    rejected = learner.reject(learning.id)
    assert rejected.stage == "failed"
    assert rejected.error == "rejected by the owner"
    assert rejected.artifact is None and rejected.ready_identity == ""
    # The identity the owner was looking at is refused too — the learning is not ready any
    # more, and "discarded" must not be a state a later request can approve out of.
    with pytest.raises(ApprovalNotReady):
        learner.approve(learning.id, ready)
    assert library.serves(CAPABILITY) is False


def test_learning_the_same_capability_twice_mints_a_second_identity_only_when_asked(recorded):
    """`POST /learn` is refused when the library already serves the capability — see
    `test_learn_e2e`. What must hold underneath is that nothing here installs a skill by itself:
    learning runs, the owner does not approve, and the library is still empty."""
    learner, library, _recorder, _spend, _held = recorded
    _run(learner)
    assert library.summaries() == []
    _run(learner, change_id="chg-2")
    assert library.summaries() == []


def test_the_real_held_out_set_grades_the_revision_twenty_for_twenty(tmp_path):
    """The number the demo quotes, against the file the owner actually wrote.

    Every other test in this module runs a three-case stand-in so the suite does not spend a minute
    starting subprocesses. This one is the measurement: 20 unseen cases, one process each, and a
    skill that was shown none of them.
    """
    learner, library, _recorder, _spend, _held = _learner(HELD_OUT_DIR)
    learning = _run(learner)
    assert learning.stage == "ready_for_review", learning.error
    last = learning.attempts[-1]
    assert last["tests"] == {"passed": 7, "failed": 0}
    assert last["held_out"] == {"passed": 20, "failed": 0}
    assert last["violations"] == []
    # And the first attempt fails all twenty, so the twenty are not passing vacuously.
    assert learning.attempts[0]["held_out"] == {"passed": 0, "failed": 20}
    assert library.serves(CAPABILITY) is False, "grading installs nothing on its own"
