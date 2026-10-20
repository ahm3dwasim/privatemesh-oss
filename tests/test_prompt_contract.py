"""The prompt/consumer contract: what the model is asked for is what the engine reads.

The defect this file exists for was measured on the real Token Factory, not imagined. The engine's
system prompts were one sentence each — `CLASSIFY_SYSTEM` was the whole of "You classify one
incoming email as a meeting-change request or not." — and named no key at all. Given the owner's
genuine café email, a real Nemotron Nano answered

    {"classification": "meeting_change_request"}

The engine read `answer.get("is_meeting_change")`, got `None`, and `None` is falsy, so a real
meeting change was dropped without a word and the run looked clean. The sample could never have
caught it: `wiring._classify` routes on the prompt TEXT and always returns the exact keys, so the
scripted model was answering a question the real one was never asked.

Three claims, and they fail independently:

* **the prompt names the object.** Every key in the phase's table appears in the rendered prompt,
  and the table is the same tuple the consumer validates against — so a key cannot be read without
  being asked for.
* **the table is the consumer's own inventory.** Source-scanned from the module, not restated by
  hand, so the table cannot quietly fall behind the code.
* **a wrong-shaped answer is LOUD.** A plausible-but-wrong object raises a named failure, records
  the phase and the keys received (never the text), and — for the watcher — leaves the message
  unseen so the next tick retries it instead of filing real mail as "not a change".
"""
from __future__ import annotations

import datetime as dt
import json
import re
import types
from pathlib import Path

import pytest

from privatemesh import changes as ch
from privatemesh import learn
from privatemesh import prompt_contract
from privatemesh.changes import ModelAnswerInvalid
from privatemesh.prompt_contract import AnswerKey, missing_required, render_system_prompt

from fakes_v2 import Harness, change_email

CHANGES_SOURCE = Path(ch.__file__).read_text()

#: The variable names `changes.py` binds a MODEL ANSWER to. Scanning `.get("key")` on exactly these
#: is what makes the inventory below the code's rather than the author's: they are never bound to
#: engine state, so a key read off one of them is a key the model was supposed to have sent.
_ANSWER_VARIABLES = ("answer", "choice", "draft", "query")


def _scanned_top_level_keys() -> set[str]:
    """Every string-literal key `changes.py` reads off a model answer."""
    found: set[str] = set()
    for name in _ANSWER_VARIABLES:
        pattern = rf'\b{name}\b[^\n]{{0,24}}?\.get\(\s*["\'](\w+)["\']'
        found |= set(re.findall(pattern, CHANGES_SOURCE))
    return found


def _engine_prompts() -> dict[str, str]:
    return {"classify": ch.CLASSIFY_SYSTEM, "extract": ch.EXTRACT_SYSTEM,
            "query": ch.QUERY_SYSTEM, "choose": ch.CHOOSE_SYSTEM, "draft": ch.DRAFT_SYSTEM}


def _learn_prompts() -> dict[str, str]:
    return {"learn.generate": learn.GENERATE_SYSTEM, "learn.review": learn.REVIEW_SYSTEM}


def _all_prompts() -> dict[str, str]:
    return {**_engine_prompts(), **_learn_prompts()}


# ---------------------------------------------------------------- the prompt names the object ---
@pytest.mark.parametrize("phase", sorted(_all_prompts()))
def test_the_system_prompt_names_every_key_its_consumer_reads(phase):
    """Parse the prompt; every key in the shared table has to be in it.

    This is the assertion that would have failed before the fix: the old classify prompt named no
    key at all, so `is_meeting_change` — the one thing the engine reads off it — was absent.
    """
    prompt = _all_prompts()[phase]
    keys = ch.ANSWER_KEYS.get(phase) or learn.GENERATE_KEYS if phase != "learn.review" \
        else learn.REVIEW_KEYS
    assert keys, f"{phase} has no key table"
    for key in keys:
        assert f'"{key.name}"' in prompt, (
            f"the {phase} prompt never names {key.name!r}, which its consumer reads — a model "
            f"cannot return a key it was not asked for")


@pytest.mark.parametrize("phase", sorted(_all_prompts()))
def test_the_prompt_tells_the_model_to_reply_with_the_object_only(phase):
    """The format instruction, not just the keys: prose around the object is what the repair
    attempt exists to undo, and it is cheaper to ask once than to re-ask."""
    assert prompt_contract.JSON_ONLY in _all_prompts()[phase]


def test_the_table_is_exactly_what_the_engine_reads_off_a_model_answer():
    """Source-scanned equality, so the table cannot fall behind the consumer.

    Equality rather than a subset check in both directions: a key the engine reads and the table
    omits is the original bug; a key the table lists and the engine never reads is a prompt asking
    for something nobody uses, which is noise a real model will happily spend tokens on.
    """
    table = {k.name for keys in ch.ANSWER_KEYS.values() for k in keys}
    assert _scanned_top_level_keys() == table, (
        "the engine's reads and the prompt's key table disagree — "
        f"read but unlisted: {sorted(_scanned_top_level_keys() - table)}; "
        f"listed but unread: {sorted(table - _scanned_top_level_keys())}")


def test_the_nested_keys_the_engine_reads_are_named_in_their_phase_prompt():
    """The sub-keys inside `options`, `fragments` and `rejected`.

    They are read one level down (`option["start"]`, `f.get("source")`, `item.get("reason")`), so
    the scan above cannot see them; what matters is the same thing — the prompt names them, and the
    form text for the parent key is where they are named.
    """
    nested = {"extract": ("option", ("start", "end")),
              "draft": ("f", ("source", "text")),
              "choose": ("item", ("index", "reason"))}
    for phase, (variable, expected) in nested.items():
        scanned = set(re.findall(rf'\b{variable}\b\[?\s*["\'](\w+)["\']', CHANGES_SOURCE))
        scanned |= set(re.findall(rf'\b{variable}\.get\(\s*["\'](\w+)["\']', CHANGES_SOURCE))
        for name in expected:
            assert name in scanned, (
                f"{phase}'s consumer no longer reads {name!r} off {variable!r}; this test is "
                f"stale rather than the code being wrong")
            assert name in _all_prompts()[phase], (
                f"{phase}'s consumer reads {name!r} off {variable!r} and the prompt never names it")


def test_the_rendered_prompt_marks_optional_keys_and_the_required_ones_are_the_dangerous_ones():
    """`required` is a claim about the FALLBACK, and it is load-bearing in both directions.

    A key is required when its absence makes the engine act on a different answer than the model
    gave — `None` off `is_meeting_change` is "not a change". Marking a key required when its
    fallback is the same answer would make the engine refuse good answers for being terse.
    """
    required = {k.name for k in ch.ANSWER_KEYS["classify"]}
    assert required == {"is_meeting_change", "is_refill_confirmation"}
    assert "(optional)" in ch.DRAFT_SYSTEM
    meeting_line = next(l for l in ch.CLASSIFY_SYSTEM.splitlines() if "is_meeting_change" in l)
    assert "(optional)" not in meeting_line, (
        "is_meeting_change must stay required — a model that answers it wrong must not be read "
        "as a quiet no")
    # `is_refill_confirmation` is the one classify key that IS optional, and for the reason the
    # docstring above states: its fallback (absent -> "not a refill") is the same negative an
    # explicit `false` would give, which is exactly what makes a key safe to mark optional.
    refill_line = next(l for l in ch.CLASSIFY_SYSTEM.splitlines()
                       if "is_refill_confirmation" in l)
    assert "(optional)" in refill_line, (
        "is_refill_confirmation's absence is meant to read as 'not a refill', so it has to be "
        "marked optional or a model that never names it would be treated as a malformed answer")
    # `is_venue` is required for the live run's reason: a missing one made the engine present a
    # station map as a researched venue. `venue_index` null with no answer to "was it a venue"
    # is not the same answer as "it was not a venue", so the key is not optional.
    assert {k.name for k in ch.ANSWER_KEYS["choose"]} >= {"venue_index", "is_venue"}
    assert '"is_venue"' in ch.CHOOSE_SYSTEM and "(optional)" in ch.CHOOSE_SYSTEM
    # The venue's name and address are NOT keys and must not come back. They were the one place on
    # the calendar path a model authored a value end to end, and the address travels to every guest
    # the invite notifies; the engine renders both from the result `venue_index` names.
    assert not ({k.name for k in ch.ANSWER_KEYS["choose"]} & {"name", "address"}), (
        "the choose phase is asked to author a place again — it may only select one")
    # The optional ones are optional for a stated reason: the fallback is the same answer.
    optional = {k.name for keys in ch.ANSWER_KEYS.values() for k in keys if not k.required}
    assert optional == {"include_domains", "rejected", "rejected_times", "template_body",
                        "is_refill_confirmation"}


# ---------------------------------------------------------------- a wrong answer is LOUD -------
def test_the_measured_defect_is_now_a_named_failure():
    """The exact payload the real Nemotron Nano sent, against the real engine.

    `{"classification": "meeting_change_request"}` is a perfectly plausible answer to the question
    the old prompt asked. It is not the object the engine reads, and the difference has to surface
    as a failure rather than as `None` travelling on as "this email is not a meeting change".
    """
    h = Harness(messages=[change_email("m1")],
                script={"raw": {"classify": '{"classification": "meeting_change_request"}'}})
    with pytest.raises(ModelAnswerInvalid) as caught:
        h.engine.classify(h.mail.messages[0])
    assert caught.value.phase == "classify"
    assert caught.value.missing == ("is_meeting_change",)
    assert caught.value.received == ("classification",)


def test_an_invalid_answer_is_audited_by_phase_and_keys_never_by_text():
    """What is recorded has to be enough to diagnose the shape and nothing more.

    The keys received are a schema fact; the answer's text is the owner's mail, and the audit chain
    is the last place it may appear.
    """
    secret = "the owner's dentist appointment is at 4"
    h = Harness(messages=[change_email("m1")],
                script={"raw": {"classify": f'{{"note": "{secret}"}}'}})
    with pytest.raises(ModelAnswerInvalid):
        h.engine.classify(h.mail.messages[0])

    rows = h.audit.rows(event="changes.model_answer_invalid")
    assert len(rows) == 1, rows
    detail = json.loads(rows[0]["detail"])
    assert detail["phase"] == "classify"
    assert detail["received"] == ["note"]
    assert detail["missing"] == ["is_meeting_change"]
    assert secret not in rows[0]["detail"], "the audit recorded the model's text"


def _base_script():
    """A well-formed answer for every phase, so a test can break exactly ONE of them.

    Without this the flow stops at the first malformed phase and the assertion proves nothing about
    the phase under test — the point is that `choose` fails on `choose`'s keys, not that `query`
    failed first.
    """
    from fakes_v2 import at
    return {"classify": {"is_meeting_change": True},
            "extract": {"options": [{"start": at("16:00"), "end": at("16:45"), "phrase": "4"},
                                    {"start": at("15:00"), "end": at("15:45"), "phrase": "3"}],
                        "request_excerpt": "tomorrow at 3 or 4?"},
            "query": {"query": "cafe near King's Cross", "include_domains": []},
            "choose": {"venue_index": None, "is_venue": False,
                       "reason_sentence": "nothing supports a venue", "rejected": []},
            "draft": {"template_body": "Hi,", "fragments": [{"source": "time",
                                                             "text": "15:00 works."}]}}


@pytest.mark.parametrize("phase,bad,missing", [
    ("extract", {"proposed_times": []}, ("request_excerpt", "options")),
    ("query", {"search": "café hours"}, ("query",)),
    ("choose", {"venue": "The Standard"}, ("venue_index", "is_venue", "reason_sentence")),
    # No `subject`, deliberately: the reply's subject is `Re: <the thread's subject>`, which the
    # engine derives, so a draft that returns one is returning a key nobody reads.
    ("draft", {"subject": "Re: Our usual cafe has closed", "body": "see you then"}, ("fragments",)),
])
def test_every_engine_phase_turns_a_wrong_shaped_answer_into_a_named_failure(phase, bad, missing):
    """One contract test per phase, each with a plausible answer that uses the WRONG key names —
    the failure mode is a model inventing a sensible schema, not a model returning garbage."""
    script = _base_script()
    script["raw"] = {phase: json.dumps(bad)}
    h = Harness(messages=[change_email("m1")], script=script)
    change = h.engine.classify(h.mail.messages[0])
    with pytest.raises(ModelAnswerInvalid) as caught:
        h.engine.prepare(change.id)
    assert caught.value.phase == phase
    assert set(caught.value.missing) == set(missing)


def test_a_later_phase_running_the_engine_is_not_reached_by_a_wrong_query_answer():
    """A malformed answer stops the flow where it happened.

    The point is not that it raises — it is that nothing downstream acts on the `None`/`[]` the
    engine would otherwise have read. A proposal built from a query the model never gave is a
    proposal the owner would have approved believing it was researched.
    """
    script = _base_script()
    script["raw"] = {"query": '{"q": "cafe near Kings Cross"}'}
    h = Harness(messages=[change_email("m1")], script=script)
    change = h.engine.classify(h.mail.messages[0])
    with pytest.raises(ModelAnswerInvalid):
        h.engine.prepare(change.id)
    assert change.proposal is None, "a failed phase left a proposal behind"
    assert h.rec.surfaces("search") == [], "a search ran on a query the model never sent"


# ---------------------------------------------------------------- the watcher retries ----------
def _routine(tmp_path, h):
    from privatemesh.routine import Routine
    h.routine = Routine(h.engine, h.mail, path=str(tmp_path / "routine.db"), audit=h.audit)
    return h.routine


def test_an_unreadable_classification_leaves_the_message_unseen(tmp_path):
    """The watcher's half of the measured defect: real mail must not be filed as "not a change".

    Before the fix `classify` returned `None` for an answer it could not read, and the watcher read
    `None` as "not a change" and marked the message seen — permanently, since the seen-set is on
    disk. Nobody is watching an always-on run, so that drop was silent and final.
    """
    h = Harness(messages=[change_email("m1")],
                script={"raw": {"classify": '{"classification": "meeting_change_request"}'}})
    routine = _routine(tmp_path, h)

    assert routine.tick() == []
    assert not routine.seen("m1"), "an unreadable answer was filed as a non-change"
    assert routine.attempts("m1") == 1


def test_the_watcher_retries_and_then_gives_up_with_a_recorded_reason(tmp_path):
    """Bounded, and honest about stopping: three ticks, then seen WITH the reason on the chain."""
    from privatemesh.routine import MAX_CLASSIFY_ATTEMPTS

    h = Harness(messages=[change_email("m1")],
                script={"raw": {"classify": '{"classification": "meeting_change_request"}'}})
    routine = _routine(tmp_path, h)

    for _ in range(MAX_CLASSIFY_ATTEMPTS - 1):
        routine.tick()
        assert not routine.seen("m1")

    routine.tick()
    assert routine.seen("m1"), "the watcher never stopped asking"
    assert routine.attempts("m1") == MAX_CLASSIFY_ATTEMPTS
    assert "is_meeting_change" in routine.unreadable_reason("m1"), routine.unreadable_reason("m1")

    assert len(h.audit.rows(event="routine.classify_retry")) == MAX_CLASSIFY_ATTEMPTS - 1
    abandoned = h.audit.rows(event="routine.classify_abandoned")
    assert len(abandoned) == 1
    assert "is_meeting_change" in abandoned[0]["detail"]

    # And it is genuinely finished with: a fourth tick does not ask again.
    assert routine.tick() == []
    assert routine.attempts("m1") == MAX_CLASSIFY_ATTEMPTS


def test_a_readable_answer_is_still_classified_once_and_marked_seen(tmp_path):
    """The retry path must not loosen the ordinary one: a good answer is still one change, seen."""
    h = Harness(messages=[change_email("m1")], script={"classify": {"is_meeting_change": True}})
    routine = _routine(tmp_path, h)
    assert [c.message_id for c in routine.tick()] == ["m1"]
    assert routine.seen("m1")
    assert routine.attempts("m1") == 0
    assert routine.tick() == []


def test_a_real_non_change_is_still_marked_seen_without_being_retried(tmp_path):
    """"Not a change" and "I could not tell" are different answers and must stay different."""
    h = Harness(messages=[change_email("m1")], script={"classify": {"is_meeting_change": False}})
    routine = _routine(tmp_path, h)
    assert routine.tick() == []
    assert routine.seen("m1"), "a genuine non-change is not retried forever"
    assert routine.attempts("m1") == 0


# ---------------------------------------------------------------- the learning loop ------------
class _StubLearner:
    """`_envelope` reads only the clock, so the envelope contract needs no model at all."""

    def __init__(self, answer):
        self._answer = answer

    _clock = staticmethod(lambda: dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc))


def test_a_generation_missing_the_envelope_keys_is_a_named_failure():
    learner = learn.Learner.__new__(learn.Learner)
    with pytest.raises(ModelAnswerInvalid) as caught:
        learn.Learner._envelope(learner, {"thoughts": "here is some code"}, "v1")
    assert caught.value.phase == "learn.generate"
    assert set(caught.value.missing) == {"module_code", "tests_code", "manifest"}


def test_a_manifest_that_declares_nothing_is_a_named_failure_not_a_default():
    """The silent default in the learning loop: a `manifest` with no keys used to be filled from
    `INPUT_SCHEMA` and `()`, so a capability could declare nothing and still be graded — and the
    owner's approval would bind to a manifest the model never wrote."""
    learner = learn.Learner.__new__(learn.Learner)
    with pytest.raises(ModelAnswerInvalid) as caught:
        learn.Learner._envelope(learner, {"module_code": "def run(i): return {}",
                                          "tests_code": "def test_x(): assert True",
                                          "manifest": {}}, "v1")
    assert caught.value.phase == "learn.generate"
    assert set(caught.value.missing) == {f"manifest.{k}" for k in learn.MANIFEST_KEYS}


def test_a_review_with_no_verdict_is_rejected_by_name_not_by_silence():
    """A malformed verdict used to be coerced to "reject" — the safe DIRECTION and the wrong
    REPORT, because it reads as a reviewer that looked and said no. The handler must name the real
    problem so the owner does not go hunting for a fault in code that was never judged."""
    learner = learn.Learner.__new__(learn.Learner)
    learner.reviewer_model = learn.REVIEWER_MODEL
    learner._json = lambda *a, **k: {"decision": "looks fine to me"}
    manifest = learn.SkillManifest(
        name=learn.CAPABILITY, version="v1", purpose=learn.CAPABILITY_PURPOSE,
        module=learn.CAPABILITY, entry="run", expires="2027-01-01T00:00:00+00:00",
        inputs=learn.INPUT_SCHEMA, outputs=learn.OUTPUT_SCHEMA,
        reads=tuple(sorted(learn.INPUT_SCHEMA["properties"])), proposes=())
    envelope = {"code": "def run(i): return {}", "tests": "def test_x(): assert True",
                "manifest": manifest, "never": []}
    graded = {"tests": {"passed": 1, "failed": 0}, "held_out": {"passed": 1, "failed": 0},
              "violations": []}
    # `rule=""` is the capability case, and it is not decoration: `_review` asks a taught skill's
    # reviewer a different question (it is handed the owner's sentence and the graded invariants),
    # so a stub without the field is a stub of an object the code cannot receive.
    out = learn.Learner._review(learner, types.SimpleNamespace(id="l1", rule=""), envelope, graded)
    assert out["verdict"] == "reject", "a malformed verdict must not approve"
    assert "verdict" in " ".join(out["reasons"]), out["reasons"]
    assert "decision" in " ".join(out["reasons"]), (
        "the reason must name what actually arrived, so the owner can tell a broken answer from a "
        f"reviewer that said no: {out['reasons']}")
