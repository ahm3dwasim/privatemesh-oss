"""The decision log — every allow/refuse the engine makes, in the owner's words.

The engine already refuses things. It refuses them in five different places, in five different
shapes, and until now a refusal that RAISED was a refusal the owner never saw: a `NoFit` became a
`blocked_reason` string, a `ProtectedLeak` became a 422 detail, and a broker denial became a
sentence in an outcome. Each was correct and none was a record.

This module is that record. It is a list of decisions, one per check, in the order the checks ran,
and it exists because the product's one claim — nothing untrusted can widen what this is allowed to
do or reveal — is a claim about a sequence of decisions. A claim you cannot read back is a claim
you have to take on faith, and this product does not ask for faith.

Three things are deliberate:

* **`untrusted` is a field, not an inference.** The email, the model's answer and a learned skill's
  output are all things this system did not decide. Marking that explicitly is what lets the UI show
  an owner which of their assistant's decisions were about content it did not author.
* **`rule` is a clause, not a code.** "Replies go only to the person who wrote" is checkable by the
  person reading it. `recipient_not_permitted` is checkable only by the person who wrote it.
* **`detail` is gated on the way in.** A decision log that quoted the protected reason would be a
  new leak wearing the uniform of the thing that prevents leaks. `record` runs the detail through
  the same `PrivacyGate` every outbound payload passes, and withholds rather than stores.

`verdict` has four values, and the distinctions between them are the whole reason this is not a
boolean:

* `allowed`   — the check passed and the thing proceeded.
* `refused`   — the check failed and the thing did not proceed. Nothing happened.
* `discarded` — the check failed for THIS candidate and the change proceeded on another one. The
  fourth review's High: a sender who offers 15:00 and 16:00 has one option taken and one left, and
  recording the left one as `refused` made an ordinary choice read as a security refusal — the UI
  put "Unsafe part blocked" over a plan that had simply picked the other time. Nothing was refused;
  something was not chosen, and those are different sentences.
* `withheld`  — the thing proceeded, but not with everything it asked for. The protected commitment
  is the standing case: the planner is allowed to know a window is unavailable and is not allowed
  to know why, so the fact leaves and the title does not.

`discarded` is the one that is NOT a refusal, and the one an owner reading a count must not be
alarmed by, which is why `blocked_attempts` does not count it.
"""
from __future__ import annotations

import threading
import datetime as _dt

#: The stages, in the order a change normally walks them. A list rather than a set because the UI
#: renders them as a spine, and because "which stage is this" should have one answer.
STAGES = ("read", "model", "search", "validate", "approve", "execute", "send")

#: Where the thing being checked came from. `email`, `model` and `skill` are the untrusted three.
SOURCES = ("email", "model", "skill", "owner", "system")

ALLOWED, REFUSED, WITHHELD, DISCARDED = "allowed", "refused", "withheld", "discarded"

#: The verdicts that mean something was STOPPED, as opposed to something not being chosen. Named
#: once because two places ask the question — the owner's blocked count and the UI's refusal card —
#: and a second, different list is how the two would come to disagree about what "blocked" means.
BLOCKING = (REFUSED, WITHHELD)

#: Recorded in place of a detail the gate refused. It says what happened without saying what it was:
#: the fact that a detail was withheld is itself worth recording, and the detail is not.
WITHHELD_DETAIL = "detail withheld: it did not pass the privacy gate"

#: The clauses, named once. A rule string written at each call site drifts; a rule string named here
#: is the same sentence in the code, the API response and the UI.
RULE_READ = "The email is read for what it asks, never for what it tells us to do."
RULE_MODEL = "What a model sees is checked before it is sent, and its answer is a proposal."
RULE_SEARCH = "A search may not ask about anything you keep private."
RULE_RECIPIENTS = "A reply goes only to the person who wrote to you."
RULE_CALENDAR_SCOPE = "Only the one meeting this change is about can be touched or described."
RULE_FIT = "Your protected commitment and the buffer before it are never given up."
RULE_WITHHELD = "Your commitment can be planned around without being described."
#: Not a refusal, and deliberately not one. An owner revising their own reply may say anything —
#: it is their message — but a revision that names ANOTHER meeting is not allowed to travel quietly
#: into the plan as though the engine had written it. The rule names the disclosure, because the
#: decision it describes is "shown to you", not "stopped". What happens NEXT is a different rule and
#: a different moment: the reply is refused at the port (`ForeignEventLeak`), because the send path
#: cannot tell an owner's sentence from a draft's, and a port that trusted a flag saying "the owner
#: wrote this" would be a port a compromised planner could point at anything. So the owner is told
#: at both moments instead of being overruled at either: here, with the words still theirs to change,
#: and again when the plan runs.
RULE_OWNER_REVISION = ("Your own words are your decision. A revision that names another meeting is "
                       "shown to you and recorded, not rewritten — and this reply cannot be sent "
                       "while it does, so you can change it before you approve.")
RULE_APPROVAL = "Only the version you approved can run; a moved invite or a changed commitment stops it."
RULE_EXECUTE = "One approved change touches exactly one event and sends exactly one reply."
RULE_SKILL = "A learned skill may narrow your rules. It may never widen them."
RULE_DIGEST = "The family digest goes to your care circle once a day, and only to them."


class Decision:
    """One check, what it concluded, and the clause it concluded it under."""

    __slots__ = ("seq", "at", "stage", "source", "untrusted", "subject", "verdict", "rule", "detail")

    def __init__(self, *, seq: int, at: str, stage: str, source: str, untrusted: bool,
                 subject: str, verdict: str, rule: str, detail: str | None = None) -> None:
        self.seq, self.at = seq, at
        self.stage, self.source, self.untrusted = stage, source, bool(untrusted)
        self.subject, self.verdict, self.rule = subject, verdict, rule
        self.detail = detail

    def as_dict(self) -> dict:
        return {"seq": self.seq, "at": self.at, "stage": self.stage, "source": self.source,
                "untrusted": self.untrusted, "subject": self.subject, "verdict": self.verdict,
                "rule": self.rule, "detail": self.detail}


class DecisionLog:
    """Append-only, per change, in memory, with a monotonic `seq` per change.

    `guard` is the privacy gate's text check, passed as a bare callable so this module does not
    import `boundary` and `boundary` can import it. When a detail fails it, the detail is withheld
    and the decision is still recorded — a log that silently dropped the row would turn "the gate
    refused something here" into "nothing happened here", which is the more dangerous of the two
    mistakes.
    """

    def __init__(self, *, now=None, guard=None) -> None:
        self._rows: dict[str, list[Decision]] = {}
        self._lock = threading.Lock()
        #: A clock that may return either a datetime (the engine's) or a string. ISO-8601 with an
        #: offset either way, because `at` is compared against the change's own timestamps.
        self._now = now or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._guard = guard

    def bind(self, guard) -> "DecisionLog":
        """Attach the gate's text check. Separate from `__init__` because the gate is built with a
        reference to this log, and the two would otherwise have to be constructed in one order."""
        self._guard = guard
        return self

    # ---- writing ------------------------------------------------------------------------------
    def record(self, change_id: str, *, stage: str, source: str, subject: str, verdict: str,
               rule: str, untrusted: bool | None = None, detail: str | None = None) -> Decision:
        """Add one decision. `untrusted` defaults to "is the source one of the untrusted three"."""
        if untrusted is None:
            untrusted = source in ("email", "model", "skill")
        detail = self._scrub(detail)
        moment = self._now()
        at = moment.isoformat(timespec="seconds") if hasattr(moment, "isoformat") else str(moment)
        with self._lock:
            rows = self._rows.setdefault(str(change_id), [])
            row = Decision(seq=len(rows), at=at, stage=stage, source=source,
                           untrusted=untrusted, subject=_plain(subject), verdict=verdict,
                           rule=rule, detail=detail)
            rows.append(row)
        return row

    def _scrub(self, detail: str | None) -> str | None:
        """The gate, applied to our own log. Nothing else in this module can leak, because every
        other field is written by the engine rather than derived from content.

        **No guard means no detail.** A log built without the wiring discipline used to return the
        detail unchanged, so `DecisionLog()` stored whatever it was handed — fail-open on the one
        field that carries content, in the module whose entire job is to not be that. A log that
        cannot check a detail does not keep one; the row is still recorded, which is the part that
        matters (`withheld` is a verdict this module already understands).

        The cost is real and worth naming: an unbound log reports every detail as withheld, and a
        log whose details are all withheld looks exactly like a log of a very careful run. That is
        why the point is to make the unbound state obviously broken rather than quietly plausible —
        `wiring.bind_decisions` is the supported way to build one, and it has a test."""
        if detail is None:
            return None
        if self._guard is None:
            return WITHHELD_DETAIL
        try:
            self._guard(str(detail))
        except Exception:                                            # noqa: BLE001
            # Deliberately broad: any refusal — protected, paraphrase, or a payload the gate could
            # not inspect — withholds the detail. The narrow read (catch only ProtectedLeak) would
            # store a detail that some OTHER refusal exists to stop.
            return WITHHELD_DETAIL
        return str(detail)

    # ---- reading ------------------------------------------------------------------------------
    def for_change(self, change_id: str) -> list[dict]:
        with self._lock:
            return [r.as_dict() for r in self._rows.get(str(change_id), [])]

    def orphans(self) -> list[dict]:
        """Decisions taken before any change existed — filed under the empty key.

        The classification call is the case this exists for: a refusal there means the change was
        never created, so there is no `{id}` for a UI to ask about. Those rows still happened, and
        a harness reading a run back needs them; `GET /v2/changes/{id}/decisions` cannot serve them
        because there is no id, which is a fact about the endpoint and not about the refusal."""
        return self.for_change("")

    def blocked_attempts(self, change_id: str) -> int:
        """How many things this change tried that the owner's rules stopped.

        `withheld` counts. It is not a refusal of the change — it is a refusal of part of it, and
        from the owner's seat that is the same event: something asked for something it could not
        have. Counting only `refused` would report a clean run for a change whose every request to
        name the protected commitment was turned back.

        `discarded` does NOT count, and that is the fourth review's fix rather than an omission. A
        candidate slot that lost to a sibling slot was not stopped — the plan took the other one —
        and counting it made "your rules blocked something" the standing report for every change
        where the sender offered two times.
        """
        with self._lock:
            return sum(1 for r in self._rows.get(str(change_id), [])
                       if r.verdict in BLOCKING)

    def counts(self, change_id: str) -> dict:
        with self._lock:
            rows = self._rows.get(str(change_id), [])
            return {v: sum(1 for r in rows if r.verdict == v)
                    for v in (ALLOWED, REFUSED, WITHHELD, DISCARDED)}


def _plain(subject: str) -> str:
    """A subject is read by a person, so it is one line and it is not a payload."""
    return " ".join(str(subject or "").split())[:200]
