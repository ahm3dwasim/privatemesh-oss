"""The agreement between a prompt and the code that reads its answer.

A model phase has two halves that have to agree: the SYSTEM PROMPT that asks for an object, and
the CONSUMER that reads keys off it. When they disagree the engine does not fail — it reads `None`
off a key nobody asked for, and `None` is falsy, so the failure arrives as a legitimate-looking
negative. That is not a hypothetical: the classify prompt used to be the single sentence "You
classify one incoming email as a meeting-change request or not." and named no key at all. A real
Nemotron Nano answered the owner's genuine café email with `{"classification":
"meeting_change_request"}`; the engine read `answer.get("is_meeting_change")`, got `None`, and
dropped a real meeting change without a word.

The fix is structural rather than a better sentence. One tuple of `AnswerKey` per phase is the
single source: `render_system_prompt` turns it into the words the model is sent, and
`missing_required` checks the answer that comes back against the same tuple. A key the consumer
starts reading is a key the prompt starts asking for, in the same edit.

This module is deliberately tiny and depends on nothing in the package, so both `changes` and
`learn` can use it without a cycle (`changes` imports `learn`, not the other way round).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AnswerKey:
    """One key a consumer reads off a model answer, and the words that tell the model so.

    `form` completes `"<name>": `, so the rendered prompt reads as the object itself.
    `required` is False only where the consumer's fallback is the SAME ANSWER rather than a quieter
    one — omitting such a key is not a wrong answer, and a prompt that marked it required would be
    strict about a difference that does not exist.
    """

    name: str
    form: str
    required: bool = True


#: The sentence that turns a described object into an instruction. One definition, used by every
#: phase, and repeated verbatim on the repair attempt so the model reads identical words twice.
JSON_ONLY = ("Reply with ONE JSON object and nothing else: no prose, no explanation, no markdown "
             "code fence.")


def render_system_prompt(opening: str, keys: tuple[AnswerKey, ...], *,
                         instructions: str = "") -> str:
    """The system prompt for one phase, rendered from `keys`.

    `opening` is the first line and is load-bearing beyond prose: `wiring.sample_reply` and the
    tests route a request to a phase by `system.startswith(marker)`, where the marker IS the
    rendered constant. Prompt and routing key are therefore the same string and cannot disagree.

    `instructions` is the phase's own judgement, stated as its own paragraph. It is a parameter
    rather than a longer `opening` for that same routing reason — a marker that has to stay a
    prefix is not the place to put a paragraph, and a phase that needs to say more than one
    sentence about HOW to answer should say it where a reader looks for it.
    """
    lines = [opening, ""]
    if instructions:
        lines += [instructions, ""]
    lines += [JSON_ONLY, "", "The object has exactly these keys:"]
    lines += [f'  "{k.name}": {k.form}' + ("" if k.required else "   (optional)") for k in keys]
    optional = any(not k.required for k in keys)
    lines += ["", "Every key is required except the ones marked (optional); do not add keys that "
                  "are not listed." if optional else
                  "Every key is required; do not add keys that are not listed."]
    return "\n".join(lines)


def missing_required(keys: tuple[AnswerKey, ...], answer: dict) -> tuple[str, ...]:
    """The required keys absent from `answer`, in the order the prompt lists them."""
    return tuple(k.name for k in keys if k.required and k.name not in answer)


class ModelAnswerInvalid(ValueError):
    """A model answered, and the answer did not carry the keys its consumer reads.

    Distinct from a parse failure: that is an envelope the engine could not read, and the single
    repair attempt is aimed at it. This is a well-formed JSON object with the WRONG SHAPE — what a
    model that was never told the keys produces, and what used to travel the rest of the pipeline
    as `None`/`""`/`[]` and read as a legitimate negative.

    It carries the phase and the keys RECEIVED, never the text: the shape of an answer is
    diagnosable, its content is the owner's mail.
    """

    def __init__(self, phase: str, missing: tuple[str, ...], received: tuple[str, ...]) -> None:
        self.phase, self.missing, self.received = phase, tuple(missing), tuple(received)
        super().__init__(f"the {phase} answer is missing {', '.join(missing)} "
                         f"(keys received: {', '.join(received) or 'none'})")
