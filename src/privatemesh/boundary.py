"""The private boundary — the protected commitment, and the gate everything outbound passes.

Two jobs, and they are the same job seen from both ends.

**In.** `BoundaryStore` holds the owner's protected commitment locally, versioned, in SQLite.
The only thing that ever leaves it is `planning_fact()`: `{start, end, buffer_minutes,
label: "protected"}`. Title, location, reason and attendees are not "hidden by the UI" — they
are not in the object the planner is handed.

**Out.** `PrivacyGate` stands in front of every outbound payload: each model call (intent,
extraction, research planning, the answer), every Tavily query, every email subject and body,
every calendar field. It refuses in three distinct ways, because they are three distinct
mistakes:

* `ProtectedLeak` — a protected value appears in the payload. Whitespace and case cannot hide it.
* `ParaphraseLeak` — the payload does not contain the words, but shares enough of the protected
  field's *content* words to be the same disclosure reworded. A marker test cannot catch this,
  and the direction says so: "a marker test alone cannot establish semantic privacy".
* `SchemaLeak` — an outbound field or fragment that no allowlist permits. This is the structural
  half: the reply body is COMPOSED from declared fragments, and a fragment whose source is not on
  the allowlist cannot become outbound text in the first place. Free-form prose is refused even
  when it happens to look harmless, because "looks harmless today" is not a property.

The gate is fail-closed in the same sense `redact.redact_secrets` is not: a payload it cannot
inspect is a payload it refuses (`EgressRefused`), never one it forwards unread.

Nothing here is advisory. `llm.TokenFactory.chat` calls `guard_model_payload` immediately after
`egress_sanitize` and before the request body is built, so a new call path cannot forget it —
which is the same reason the sanitizer lives there rather than at each call site.
"""
from __future__ import annotations

import base64
import html
import json
import re
import sqlite3
import threading
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from functools import lru_cache

from .ports import Evidence, ProtectedCommitment

LABEL = "protected"

#: What an outbound email may be BUILT from. A fragment's `source` names where its text came from;
#: if the source is not here, the text does not go out. This is the allowlist the direction asks
#: for, expressed as a closed set rather than as a review habit.
ALLOWED_FRAGMENT_SOURCES = frozenset({
    "template",       # fixed wording written for this product
    "time",           # the proposed slot, from the planning fact
    "venue",          # a researched venue's name/address, from Evidence
    "recipient",      # the sender's name, from the thread the owner selected
    "thread_subject",  # the subject of the thread being answered
    "owner_name",
    "opening_hours",  # a quoted fact from an official source
    "owner",          # words the OWNER typed into their own proposal; not model output
    # The family digest's four. Repeated here as literals rather than imported from `care` so this
    # module keeps its one-way import direction; `test_care.py` asserts the two spellings are
    # equal, which is what keeps this from becoming a second source of truth — the same arrangement
    # `RULE_MODEL_NOTE` and `decisions` already have, and for the same reason.
    "care_greeting",   # the digest's fixed opening line
    "care_happened",   # what ran today, rendered from a change's own outcome record
    "care_coming",     # what is on the calendar, rendered from the event
    "care_needs_you",  # what is waiting on the owner, rendered from the open change
    # The day an action is FOR, computed by the engine rather than written by anyone. It is the one
    # piece the digest's subject varies by, and a SUBJECT is an outbound field like any other: the
    # sixth review's first finding is that the subject reached the provider header with nothing
    # having rendered it, so it is composed from a named source here rather than handed over.
    "day",
})

#: Outbound email fields. Anything else on the wire is a field nobody reviewed.
EMAIL_FIELDS = frozenset({"to", "subject", "body", "in_reply_to"})

#: The FIXED part of every subject this product may send, as a closed set. A subject is not a
#: sentence anybody writes: it is `Re:` in front of the thread's own words, or the digest's `Today —`
#: in front of the day. The set is closed for the same reason `RECIPIENT_FIELDS` is — a subject
#: template that is not on it did not come from the composer, and "there is no code path that
#: writes one" is a fact about today's callers rather than a rule anything enforces. Two entries,
#: and a third has to be added here and be reviewed as what it is: a new sentence this product puts
#: in a header the recipient sees.
SUBJECT_TEMPLATES = frozenset({
    "Re:",        # a reply to a thread: the thread's own subject follows
    "Today —",    # the family digest: the day it is for follows
})

#: Words too common to carry meaning. Kept short on purpose: an over-long list makes the
#: paraphrase check quietly weaker, and a silently weaker privacy check is the failure this
#: module exists to prevent.
_STOPWORDS = frozenset("""
about above after again against almost alone along already also although always among another
around because before behind being below beside better between beyond both cannot could does
doing done down during each either else enough even ever every from further have having here
herself himself however into itself just least less like made make many might more most much
must myself neither never only other ought over own same should since some such than that their
theirs them themselves then there these they this those though through thus under until upon
very want well were what when where whether which while whom whose will with within without
would your yours yourself
""".split())

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_TOKEN_RE = re.compile(r"[a-z0-9']{4,}")

#: How many CONSECUTIVE content tokens of a protected value must appear consecutively in a payload
#: before it counts as the same value shortened. Two, and not one: a single shared token is what a
#: corpus looks like — the sample's own inbox says "Invoice 4471 is ready" and the benefits id ends
#: in 4471 — so a one-token rule would refuse the product's ordinary mail and the check would be
#: turned off by whoever had to ship around it. A two-token run is the shortest thing that is
#: evidence of the value rather than of the language: "118 Wentworth Road" out of
#: "Flat 3, 118 Wentworth Road, London N17 6XQ" is four tokens of that address, in order.
_PARTIAL_TOKENS = 2

#: A digit run this long is matched on its own. Five, because four digits is a year and a year on
#: its own is not evidence of anything — see `_digit_evidence`.
_LONG_NUMBER = 5

_MONTHS = frozenset((
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec").split())

#: Runnable base64. The `=` padding is optional and the run is bounded below at 16 because a
#: shorter one decodes to noise that would match nothing and cost a decode per payload.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")

#: The characters an attacker reaches for when they want a value to LOOK like the value without
#: being the bytes of it. Folded to their Latin twins before comparison. Not exhaustive by
#: construction — a homoglyph table never is — and `docs/THREAT_MODEL.md` says so out loud rather
#: than letting this read as a complete defence.
_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y",
    "х": "x", "і": "i", "ѕ": "s", "ј": "j", "һ": "h", "ԁ": "d",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "μ": "m",
    "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    "－": "-", "．": ".", "＠": "@", " ": " ", "​": "", "﻿": "",
})


class EgressRefused(RuntimeError):
    """A payload did not leave the machine. Nothing was sent.

    `kind` and `field` are the refusal in a form a PROGRAM can read, and they are additive rather
    than a replacement: the message stays the sentence a person reads, and these two say which
    surface was refused and which protected field it would have disclosed. They are empty when the
    refusal is not about one named field (a schema, an audience), which is why they default rather
    than being required — every existing raise site keeps working unchanged.

    They exist because the alternative is parsing the message. `PrivacyGate._refuse` is the only
    place that knows both values, so it is the only place that sets them; a receipt that recovered
    "which field" by string-matching a refusal sentence would be one wording change away from
    silently reporting nothing.
    """

    kind: str = ""
    field: str = ""


class ProtectedLeak(EgressRefused):
    """A protected field's value appears — verbatim, up to case and whitespace — in the payload."""


class ParaphraseLeak(EgressRefused):
    """The payload rewords a protected field: not the same string, but the same disclosure."""


class SchemaLeak(EgressRefused):
    """An outbound field or fragment source that no allowlist permits."""


class CareAudienceRefused(EgressRefused):
    """A care action named someone the owner's care circle does not cover.

    Separate from `RecipientRefused` because it is the other audience question. That one is about a
    REPLY and its answer is "whoever wrote to you"; this one is about a care action and its answer
    is "the list you wrote". An owner reading a refusal needs to know which list was consulted,
    because the fix is different: one is "this is not the sender", the other is "add them, or
    grant them this".
    """


class ForeignEventLeak(EgressRefused):
    """The payload describes a meeting other than the one this change is about.

    A sibling of `ProtectedLeak` rather than a flavour of it, because the value that would have
    left is not one of the owner's protected fields — it is somebody else's meeting, which the
    engine was never asked about and has no business describing. The refusal names WHICH event it
    would have revealed, by id, and never the text that revealed it: a receipt that quoted the
    fragment would be the leak it is reporting."""


class CalendarDayUnknown(EgressRefused):
    """A calendar write whose target day could not be determined, so it was refused.

    A sibling of `ForeignEventLeak` rather than a rounding of it, because this is the failure where
    there is no answer at all: the foreign check asks "is this another meeting's place?" and can
    only answer it about a day. Handed a start it cannot place, the fourth review's version set the
    day's window to `()` and asked the question about an EMPTY day, which answers "no" every time —
    so a malformed approved bundle carrying `Room 2, Town Hall` from another meeting reached the
    provider and completed. The empty set was not a finding of innocence; it was the check being
    unable to ask.

    The refusal direction is therefore the opposite of `ForeignEventLeak`'s: that one fires on
    evidence, this one fires on its absence, and it exists because "I could not determine the day"
    and "the day holds nothing foreign" are different states that were being spelled the same way.
    """


class CalendarRefused(EgressRefused):
    """A calendar field that is not the render of a record this build fetched.

    The sibling `ForeignEventLeak` fires when a field is PROVEN to describe another meeting;
    `CalendarDayUnknown` fires when the day cannot be determined at all. This one fires on the
    third state, which the other two have no name for: the value is neither another meeting's nor
    unplaceable — it simply came from nowhere the engine can point at. A model-authored venue is
    exactly that, and it is why neither of the other two could catch it: the field was not secret
    and not somebody else's, so the questions that were asked both answered "no" while the invite
    went out with a place nothing had published, to every guest it notifies.
    """


class RecipientRefused(EgressRefused):
    """An outbound email names someone the thread it answers did not come from.

    Separate from `SchemaLeak` because it is a different failure with a different fix: the schema
    says the SHAPE of the message is wrong, this says the AUDIENCE is. An owner reading a refusal
    needs to know which.
    """


class ApprovedArgumentsRefused(EgressRefused):
    """An outbound call carries a value the owner never approved.

    The sixth review's second and third findings, in one class. The reply's
    `In-Reply-To`/`References`/idempotency header and the calendar's `start`/`end`/`event_id`/
    `send_updates` all reached the provider from the CALLER'S arguments: the approval authorized
    one set of values and the port could write another, and nothing between them compared the two.
    That is a different failure from every other refusal in this module — the value is not
    protected, not somebody else's, and not malformed. It is simply not the one that was approved,
    and the fix is to compare against the plan rather than to recognise a bad string.
    """


#: Every recipient-bearing field an email can carry. Only `to` exists in this product — there is no
#: cc and no bcc anywhere on the path, so a message that arrives with one has been assembled by
#: something other than the composer. The list is a closed set checked against the payload rather
#: than a comment saying "we do not use bcc", because the second one does not fail a test.
RECIPIENT_FIELDS = frozenset({"to", "cc", "bcc"})

#: The calendar fields the ENGINE scheduled rather than the model wrote, and which
#: `guard_calendar_foreign` therefore does not read as prose about a meeting.
#:
#: `event_id` and `if_match_etag` are the identity of the meeting being written, which the change set
#: read back from the provider; `start` and `end` are the slot the OWNER'S OWN fit rules produced;
#: `send_updates` is an enum and `change_id` is this change's own name. None of them is a sentence
#: about a meeting, and one of them is a trap: the pinned meeting is being moved INTO a slot, so its
#: new start MAY be a time another meeting also occupies — that collision is what the 45-minute
#: buffer arithmetic exists to reason about, and refusing the write over it would refuse the product
#: working. The clock question about a slot is asked where it belongs, of the reply's PROSE, by
#: `guard_email` on the final bytes.
#:
#: The list is an exclusion rather than the three names the model writes today, because the field
#: that leaks is the one nobody remembered: `location` is the field this exists for, `attendees` and
#: `title` are the same shape, and a field added by a later builder is covered by being absent from
#: this set rather than by being added to another one.
#: `timezone` is here for the same reason `start` and `end` are: it is the zone the plan was built
#: in, read off the event the change pinned, and its content is a zone name rather than anything a
#: model wrote or another meeting said. It is EXCLUDED from the foreign-event walk and BOUND to the
#: approved bundle like every other engine field — the two are different questions and the eighth
#: review's fourth finding is what came of answering only the first.
_CALENDAR_ENGINE_FIELDS = frozenset({"event_id", "if_match_etag", "start", "end", "send_updates",
                                     "change_id", "timezone"})

#: The rule, in the words the owner reads. Imported by the decision log so the sentence is written
#: once and cannot drift between the gate that enforces it and the receipt that reports it.
RECIPIENT_RULE = "A reply goes only to the person who wrote to you."

#: The care circle, in the owner's words. The same sentence `limits.CLAUSES` carries as clause 7,
#: so the clause the owner read and the rule on the decision row are one string.
CARE_CIRCLE_RULE = "Send only to people on your care circle."

#: The one rule under which a protected value is recorded as ALLOWED through the gate. It exists so
#: an approval is visible on the spine rather than inferred from the absence of a refusal.
DISCLOSURE_APPROVED_RULE = ("You approved this disclosure: the value left in the artifact you "
                            "approved, to the care-circle members you approved it to.")


def _address(value: str) -> str:
    """The bare address from `Name <addr>` or `addr`, lowercased. Comparison is on the address
    because that is what the provider routes on — a display name is decoration an attacker picks."""
    match = re.search(r"<([^>]+)>", value or "")
    return (match.group(1) if match else str(value or "")).strip().casefold()


#: The same clauses `decisions` names, repeated here as literals because `boundary` is imported BY
#: `decisions`-adjacent code and importing back would close the cycle. `test_decisions.py` asserts
#: the two spellings are equal, which is what keeps this from being a second source of truth.
RULE_MODEL_NOTE = "What a model sees is checked before it is sent, and its answer is a proposal."
RULE_SEARCH_NOTE = "A search may not ask about anything you keep private."
RULE_CALENDAR_NOTE = "Only the one meeting this change is about can be touched or described."
RULE_SEND_NOTE = "The reply is composed from reviewed pieces and checked before it is sent."

#: Which stage of the change a refusal at each surface belongs to. A refusal is a decision like any
#: other and belongs on the spine where it happened, not in a separate error list.
_STAGE_BY_KIND = {"model": "model", "search": "search", "email": "send", "calendar": "execute"}

_RULE_BY_KIND = {"model": RULE_MODEL_NOTE, "search": RULE_SEARCH_NOTE,
                 "email": RULE_SEND_NOTE, "calendar": RULE_CALENDAR_NOTE}


def _stage_of(kind: str) -> str:
    return _STAGE_BY_KIND.get(str(kind).split(":")[0], "model")


#: Where the content a surface checks actually CAME FROM. Every row used to say `source="model"`,
#: whatever the payload contained — so the decision log could not answer the question it exists to
#: answer: which untrusted input participated in this decision. A provenance field that names the
#: same origin for everything is not provenance, it is decoration.
#:
#: The rule is the narrow true statement rather than the flattering one:
#:
#: * a model payload is COMPOSED BY THE ENGINE — the engine decides what goes in it — so the row
#:   says `system`. The untrusted inputs inside it entered through checks that name themselves
#:   (the email is read under its own row, the search query under its own).
#: * a search query is written by the model, and a reply body is composed from fragments the model
#:   drafted: `model`, untrusted, and that is a fact about the text.
#: * a recipient is the address the inbound message came from, read off the thread and checked
#:   against the provider: `email`.
#: * calendar fields are assembled by the engine from the approved plan: `system`.
#:
#: A surface that cannot name one origin says `system`/`untrusted: false`. Never a guess: a wrong
#: provenance is worse than a coarse one, because it is a wrong answer that looks like a fact.
_SOURCE_BY_KIND = {"model": "system", "search": "model", "email": "model",
                   "email:recipients": "email", "calendar": "system"}


def _source_of(kind: str) -> str:
    kind = str(kind)
    return _SOURCE_BY_KIND.get(kind) or _SOURCE_BY_KIND.get(kind.split(":")[0], "system")



def _rule_of(kind: str) -> str:
    """The clause for a refusal, plus the half that makes it a refusal about privacy.

    The decision log's clause says what is checked; a refusal under it should say why the check
    exists. Naming the boundary here is the one place that sentence is written."""
    base = _RULE_BY_KIND.get(str(kind).split(":")[0], RULE_MODEL_NOTE)
    return f"{base} Nothing about your protected commitment may pass this gate."


def _plain_query(query: str) -> str:
    """A search query as a subject line: one line, bounded, and never a place to hide a payload."""
    return " ".join(str(query or "").split())[:120]


def privacy_line(fact: dict) -> str:
    """The sentence shown beside the proposal. Built from the allowed fact, never from the
    commitment — the line an owner reads is itself composed only of what may leave."""
    hhmm = str(fact.get("start", ""))[11:16]
    return f"Your {hhmm} commitment stays protected. Its details are not included."


# ---------------------------------------------------------------- fragments --------------------
@dataclass(frozen=True)
class Fragment:
    """One piece of outbound reply text, and where it came from.

    `event_id` is the meeting this piece DERIVES FROM, and it is assigned by the engine that
    rendered the piece rather than claimed by whoever asked for it. That is the whole of the
    fourth review's structural finding: the boundary used to be a matcher that read the body and
    tried to recognise another meeting in it, so every spelling the matcher did not enumerate walked
    past — `09:15-09:45`, `fifteen past eleven`, `neuf heures quinze`, base64, full-width digits.
    A matcher that enumerates spellings loses that race by construction, because the attacker picks
    the spelling and the defender has to have thought of it first.

    Provenance inverts it. A piece is not *scanned and allowed*; it is *ineligible* unless the
    engine rendered it, and a rendered piece names the one meeting it came from. Empty is the
    honest state of a piece that is not about any meeting — a greeting, a signature — and it is
    deliberately not the same as "about some meeting we did not check".
    """
    source: str
    text: str
    event_id: str = ""


@dataclass(frozen=True)
class OutboundEmail:
    to: tuple[str, ...]
    subject: str
    body: str
    fragments: tuple[Fragment, ...] = field(default_factory=tuple)
    in_reply_to: str = ""
    template_body: str = ""
    #: The subject's own decomposition, and the sixth review's first finding is why it is here.
    #: `subject` was taken from the caller at the port and written into the provider's `Subject`
    #: header with nothing having rendered it: the body had been made an equality against its own
    #: declared fragments, and the subject had not. So the subject is held to the SAME rule as the
    #: body — `subject_template` is one of `SUBJECT_TEMPLATES` and `subject_fragments` are
    #: allowlisted pieces — and `subject_rendered()` is the only string this object is allowed to
    #: put in that header. Two fields rather than one because the pieces are the provenance claim:
    #: "Re:" says nothing, and `thread_subject` says where the words after it came from.
    subject_template: str = ""
    subject_fragments: tuple[Fragment, ...] = field(default_factory=tuple)
    #: Where `template_body` CAME FROM. A fixed sentence is only reviewable if the reviewer can see
    #: which allowlisted source it claims, and the claim is on the object rather than beside it so
    #: `guard_email` can check it on every path. Empty is the honest state of an email with no
    #: template; a non-empty template with an empty or unlisted source is a `SchemaLeak`.
    template_source: str = ""
    #: The care circle as it stood when this email was composed, for a care action. `None` for
    #: everything else. The send path re-answers the membership question against THIS object, so a
    #: member removed between composition and send cannot still be a member of the message.
    care_snapshot: object = None
    care_kind: str = ""
    #: The meeting this message IS about, and the day's OTHER meetings reduced to the fragments
    #: that would identify them: `((event_id, fragment), ...)`. Carried on the object rather than
    #: held on the gate for the same reason `care_snapshot` is — a check that reads the pin off a
    #: gate answers about whatever the gate last saw, and a reply composed about one meeting must
    #: be judged against that meeting when it is sent, not against the next one the engine plans.
    #: Empty on every path that is not about one meeting, which is the state that checks nothing
    #: and refuses nothing.
    pinned_event: str = ""
    foreign_fragments: tuple[tuple[str, str, str], ...] = ()

    def rendered(self) -> str:
        """The body this email is ALLOWED to have: the fixed sentence plus its fragments."""
        return _render(self.template_body, self.fragments)

    def subject_rendered(self) -> str:
        """The subject this email is ALLOWED to have: the fixed prefix plus its own fragments.

        The same shape as `rendered`, and deliberately not the same function: the body's template
        has a SOURCE (`template_source`) because it may be the owner's words, while the subject's
        does not because `SUBJECT_TEMPLATES` is the closed set the product's own wording lives in.
        Two claims, two checks."""
        return _render(self.subject_template, self.subject_fragments)

    def as_payload(self) -> dict:
        return {"to": list(self.to), "subject": self.subject, "body": self.body,
                "in_reply_to": self.in_reply_to}


def _render(template_body: str, fragments) -> str:
    return " ".join(p for p in ([template_body] if template_body else [])
                    + [f.text for f in fragments] if p)


def render_subject(template: str, fragments) -> str:
    """`_render`, under a name the subject's producers can call.

    The composer in `changes` has to build the SAME string `OutboundEmail.subject_rendered()` will
    accept, and the whole finding is what happens when two places build a subject. So the join is
    one function with two callers rather than a spelling each: `reply_subject` renders through it,
    the digest renders through it, and `guard_email` compares against it."""
    return _render(template, fragments)


def foreign_fragments_of(events, *, pinned: str,
                         exempt_text: str = "") -> tuple[tuple[str, str, str], ...]:
    """The day's OTHER meetings, as the fragments a body must not contain, as `((id, text), ...)`.

    `events` is the whole day. `pinned` is the meeting this message is about, by id: it is dropped,
    because its title, its place and its time are exactly what the body is supposed to state.

    Dropping it by ID is not enough on its own, and the second exclusion is the one that keeps this
    from firing on the product working: every fragment the PINNED meeting also carries is dropped
    too. Every meeting on a day is in `Europe/London`, the pinned one included, and the engine is
    entitled — and does — tell the model the owner's timezone. Marking that as another meeting's
    detail would refuse a correct reply, and a check that fires on a correct reply is a check
    somebody switches off.

    `exempt_text` is the third exclusion, and it exists because the second one is read at the WRONG
    MOMENT. `drop` is the pinned meeting as the provider serves it NOW, and by the time the reply
    reaches the port the calendar move has already happened: the meeting that was at `Old Cafe,
    King's Cross` is now at `The Standard`, so `cafe` is no longer one of ITS fragments while it is
    still a fragment of a second meeting that never moved. The reply's subject — `Re: Our usual
    cafe has closed`, the thread's own words — then matches the second meeting's place and a
    correct reply is refused. The reviewer's rule is about what a reply DISCLOSES, and a sentence
    the recipient wrote themselves is not a disclosure to them: `exempt_text` is the text of the
    message this change was raised from, read from the provider, and every fragment it carries is
    dropped for the same reason the pinned meeting's are.

    It is the CHANGE'S OWN inbound message and not the thread, deliberately: the reply goes back to
    the person who wrote that one message, and `_permitted_recipients` has already established that
    the provider's copy of it is this change's message, in this change's thread, from the pinned
    correspondent. A second message in the same thread is not evidence about who is reading."""
    pinned_event = next((e for e in events or ()
                         if str(_field_of(e, "id") or "") == pinned), None)
    drop = {fragment for _field, fragment in event_fragments(pinned_event)} \
        if pinned_event is not None else set()
    if exempt_text:
        drop |= {fragment for _field, fragment in event_fragments({"exempt": exempt_text})}
    out: list[tuple[str, str, str]] = []
    for event in events or ():
        event_id = str(_field_of(event, "id") or "")
        if event_id == pinned:
            continue
        for field, fragment in event_fragments(event):
            if fragment not in drop:
                out.append((event_id, field, fragment))
    return tuple(out)


def _field_of(event, name: str):
    """One field of an event that may be a dict or an object — the two shapes callers hold."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def foreign_event_in(text: str, foreign) -> tuple[str, str] | None:
    """`(event_id, how)` for the first fragment of another meeting this text carries, else None.

    One reader, two callers that want different things done about the same answer: the composer
    DROPS the piece, the final-bytes check REFUSES the message. Written once because the question
    is the same one — "is this text about a meeting nobody asked about" — and a second
    implementation would be a second answer to it, drifting from the first at whichever end is
    less well tested."""
    flat = _payload_flat(text)
    # The clock reader is asked once per text and only when a clock fragment is reached: it is the
    # expensive half, and most texts name no meeting but the one they are about.
    clocks = None
    for event_id, fragment_field, fragment in foreign:
        if fragment_field.endswith(":clock"):
            if clocks is None:
                clocks = time_of_day_mentions(text)
            if minutes_of(fragment) in clocks:
                return event_id, "rewritten"
            continue
        how = _how_present(fragment, flat)
        if how:
            return event_id, how
    return None


def filter_foreign_fragments(fragments, *, pinned: str, events, template_body: str = "",
                             gate=None, change_id: str = "") -> tuple[list, str, list]:
    """The reply's pieces, minus the ones that describe a meeting nobody asked about.

    Returns `(kept, template_body, dropped)` and records a refusal for each drop through the gate.
    The template is filtered by the same rule and for the same reason: it is a piece of the body,
    it is the FIRST piece of the body, and a version of this that filtered only the list would
    leave a model that writes its prose into `template_body` a channel nothing looks at.

    Dropping the PIECE rather than refusing the change is the whole design, and it is the same
    argument the corpus already makes about a guard that refuses the day: the owner asked for one
    meeting to be moved, and a model that smuggled a sentence about a different one into the draft
    must not be able to cancel the move. Refusing everything is an outage an attacker can trigger
    with one line of prose. What cannot happen is the sentence leaving, and it does not: the reply
    is composed from what remains, and `guard_email` then reads the FINAL bytes — so a piece that
    got past this filter still cannot reach the port."""
    foreign = foreign_fragments_of(events, pinned=pinned) if pinned else ()
    kept, dropped = [], []
    for fragment in fragments or ():
        match = foreign_event_in(getattr(fragment, "text", ""), foreign) if foreign else None
        if match is None:
            kept.append(fragment)
            continue
        event_id, how = match
        dropped.append({"source": getattr(fragment, "source", ""), "event": event_id})
        if gate is not None:
            gate.note_foreign_fragment_refused(
                source=getattr(fragment, "source", ""), event=event_id, how=how,
                field="body", change_id=change_id)
    template = template_body or ""
    if template and foreign:
        match = foreign_event_in(template, foreign)
        if match is not None:
            event_id, how = match
            dropped.append({"source": "template", "event": event_id})
            if gate is not None:
                gate.note_foreign_fragment_refused(source="template", event=event_id, how=how,
                                                   field="body", change_id=change_id)
            template = ""
    return kept, template, dropped


def reply_catalogue(*, pinned: str, slot_label: str = "", venue_name: str = "",
                    venue_address: str = "", recipient_name: str = "", owner_name: str = "",
                    thread_subject: str = "", opening_hours: str = "",
                    greeting: str = "") -> tuple[Fragment, ...]:
    """Every sentence a reply to THIS change is allowed to contain, rendered by the engine.

    The engine writes the bytes; whoever drafts the reply may only ask for a piece that is already
    in here. That is what makes model-authored free text INELIGIBLE rather than scanned: there is no
    path by which a string the engine did not render becomes part of the body, so the question
    "does this sentence describe another meeting?" is answered before the sentence exists rather
    than after it has been written.

    Every fact is read from the change's own plan — the slot it proposed, the venue it chose, the
    person who wrote, the thread it answers — so a fact about a DIFFERENT meeting is not in the
    catalogue and cannot be asked for. Each meeting-derived piece carries the pinned event id;
    the greeting carries none, and neither does the owner's name, because neither is about a
    meeting and pretending otherwise would be a provenance claim the engine cannot make.

    `recipient_name` and `thread_subject` are accepted and unused: the 2026-09-22 live run's draft
    picked both as BODY fragments and the composer duly rendered them — the greeting already carries
    the correspondent's name ("Hi Alex,"), so a second, separate "Alex Doe" fragment produced
    "Hi Alex, Alex Doe …", and the thread's subject line, stated as a body sentence, read as a
    fragment of a header rather than of a reply. Both facts are real and both are already rendered
    elsewhere — the greeting carries the name, `reply_subject_fragments` carries the subject into
    the actual `Re:` header — so putting them in the BODY catalogue only gave a compliant-but-illegal
    draft two more ways to write a sentence a human would not. The parameters stay so a caller that
    already names them keeps working; nothing here turns them into a fragment.
    """
    pieces: list[Fragment] = []
    if greeting:
        # Fixed product wording plus the owner's own name. Not about a meeting, so no event id.
        pieces.append(Fragment(source="template", text=greeting))
    if slot_label:
        # The proposed slot, stated the way the reply states it. The instant is the engine's own.
        pieces.append(Fragment(source="time", text=f"{slot_label} works for me.", event_id=pinned))
    place = ", ".join(p for p in (venue_name, venue_address) if p)
    if place:
        pieces.append(Fragment(source="venue", text=f"{place}.", event_id=pinned))
    if opening_hours:
        pieces.append(Fragment(source="opening_hours", text=opening_hours, event_id=pinned))
    if owner_name:
        pieces.append(Fragment(source="owner_name", text=owner_name))
    return tuple(pieces)


def eligible_reply_fragments(proposed, catalogue, *, pinned: str) -> tuple[list, list]:
    """`(kept, ineligible)`, where KEPT are the catalogue's own fragments and never the draft's.

    Two rules, and the second is the one the fourth review asked for:

    * a piece the engine did not render is ineligible — there is no catalogue entry for it, so
      there is no text to keep. It is not refused-and-therefore-absent either: the caller keeps
      its own copy for the receipt, which is how "the model wrote something that could not become
      a body" stays visible without the words becoming a body;
    * a piece whose provenance is a meeting other than the pinned one is ineligible, and this is
      asked of the ENGINE's id rather than of the text. The reply is about one meeting, so a piece
      derived from another is not part of this reply whatever it says.

    Returning the catalogue's object rather than the caller's is the point of the function. A
    caller that passed the right words and got back its OWN object would still be handing bytes to
    the renderer; a caller that gets back the engine's object has had its bytes replaced by the
    engine's, and the two are equal only because the lookup said so."""
    by_key = {(piece.source, piece.text): piece for piece in catalogue or ()}
    kept, ineligible = [], []
    for frag in proposed or ():
        source = str(getattr(frag, "source", "") or "")
        text = str(getattr(frag, "text", "") or "")
        found = by_key.get((source, text))
        if found is None or (found.event_id and found.event_id != pinned):
            ineligible.append(frag)
            continue
        kept.append(found)
    return kept, ineligible


def _check_fragment_sources(frags, *, what: str) -> None:
    """Every piece of an outbound message must be a `Fragment` from an allowlisted source.

    One function for the body's pieces and the subject's, because the question is the same one and
    a second spelling of it is a second thing to keep in step — the arrangement `compose_email`
    already makes about the body template, applied to the field the sixth review found had no
    question asked of it at all.
    """
    for frag in frags:
        if not isinstance(frag, Fragment):
            raise SchemaLeak(f"outbound email {what} is {type(frag).__name__}, not a Fragment")
        if frag.source not in ALLOWED_FRAGMENT_SOURCES:
            raise SchemaLeak(
                f"{what} source '{frag.source}' is not on the outbound allowlist "
                f"({', '.join(sorted(ALLOWED_FRAGMENT_SOURCES))}) — text from an unlisted source "
                f"does not leave the machine")


def compose_email(*, to, subject_template: str, subject_fragments=(), fragments,
                  gate: "PrivacyGate | None" = None,
                  in_reply_to: str = "", template_body: str | None = None,
                  permitted=None, change_id: str = "", artifact: str = "",
                  care_kind: str = "", template_source: str = "",
                  care_snapshot=None, pinned_event: str = "",
                  foreign_events=(), catalogue=None, exempt_text: str = "") -> OutboundEmail:
    """Build the only kind of email this system may send: declared fragments, checked sources.

    `body` is the fragments rendered in order. When a caller wants a fixed sentence around them
    (a greeting, a signature), it passes `template_body` — still a fragment source, still
    checked — rather than handing over free-form prose.

    The SUBJECT is built the same way and has no `subject=` parameter at all. That absence is the
    sixth review's first finding: with the finished string accepted, the port took the caller's
    subject and wrote it into a header the recipient reads, and the only thing standing between a
    novel subject and the wire was a scan for protected values — which cannot see a subject that is
    merely untrue. A caller now says which FIXED PREFIX it is using (`SUBJECT_TEMPLATES`) and which
    pieces follow it, and `guard_email` refuses a subject that is not that render.

    `permitted` is the audience this reply is allowed to reach, and it is checked HERE as well as
    at the port: the second check is the one that cannot be skipped, and this one is the one that
    names the mistake while the stack still says which step built the message.

    The check is recorded at stage `validate`, not `send`, because at this moment nothing is being
    sent — this is a proposal being checked before the owner ever sees it. The port records its own
    check at `send`. Two moments, two rows, and the spine reads in the order things happened.
    """
    frags = tuple(fragments)
    _check_fragment_sources(frags, what="fragment")
    subj_template = str(subject_template or "")
    if subj_template and subj_template not in SUBJECT_TEMPLATES:
        raise SchemaLeak(
            f"outbound email subject template {subj_template!r} is not one of the subject "
            f"templates this build writes ({', '.join(sorted(SUBJECT_TEMPLATES))}) — a subject is "
            f"not a sentence anybody composes, and one that is not on this list did not come from "
            f"the composer")
    subj_frags = tuple(subject_fragments)
    _check_fragment_sources(subj_frags, what="subject fragment")
    # The template is a fragment too. It renders FIRST in the body, so a `template_body` nobody
    # source-checked was a second, unchecked channel into the same bytes — and the fix is the same
    # question asked of the same object rather than a second rule: which allowlisted source did
    # this text come from? `template_source` is required whenever there is a template at all.
    template = template_body or ""
    if template and template_source not in ALLOWED_FRAGMENT_SOURCES:
        raise SchemaLeak(
            f"outbound email template source {template_source!r} is not on the outbound allowlist "
            f"({', '.join(sorted(ALLOWED_FRAGMENT_SOURCES))}) — a fixed sentence still has to come "
            f"from somewhere this build listed")
    # ELIGIBILITY, once the allowlist has had its say and before anything is rendered. The ORDER is
    # load-bearing: a source that is not on the list at all is a REFUSAL — the shape of the message
    # is wrong and the owner has to hear about it — while a piece whose source IS listed but whose
    # words the engine never rendered is INELIGIBLE, which is a drop. Folding the two together
    # would have turned "the model reached for a source nobody allows" into a quiet omission, and
    # that refusal is the one that tells the owner their draft tried something.
    #
    # `catalogue` is the engine's own renderings for THIS change. A piece that is not in it never
    # reaches the renderer: the caller's words are replaced by the engine's, which is the whole
    # difference between a boundary that scans text and one that composes it.
    if catalogue is not None:
        frags, ineligible = eligible_reply_fragments(frags, catalogue, pinned=pinned_event)
        if template:
            # The template is a piece of the body like any other, and it renders FIRST, so it is
            # asked the same question through the same function. Asked here rather than with an
            # inline membership test because the red-team table needs ONE line to remove: a second
            # spelling of the same rule would be a second thing to remember to remove, and a
            # mutation control that removed only one of them would report a boundary that was
            # still standing.
            kept, refused = eligible_reply_fragments(
                [Fragment(source=str(template_source or "template"), text=template)],
                catalogue, pinned=pinned_event)
            if kept:
                template = kept[0].text
            else:
                ineligible.extend(refused)
                template = ""
        if gate is not None:
            for frag in ineligible:
                gate.note_ineligible_fragment(source=frag.source, change_id=change_id,
                                              pinned=pinned_event)
    email = OutboundEmail(to=tuple(to), subject=_render(subj_template, subj_frags),
                          body=_render(template, frags), fragments=frags,
                          in_reply_to=in_reply_to, template_body=template,
                          template_source=template_source if template else "",
                          subject_template=subj_template, subject_fragments=subj_frags,
                          care_snapshot=care_snapshot, care_kind=care_kind,
                          pinned_event=pinned_event,
                          foreign_fragments=(foreign_fragments_of(
                              foreign_events, pinned=pinned_event,
                              exempt_text=exempt_text) if pinned_event else ()))
    if gate is not None:
        gate.guard_email(email, change_id=change_id, permitted=permitted, stage="validate",
                         artifact=artifact, care_kind=care_kind)
    return email


# ---------------------------------------------------------------- the store --------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS protected_commitments(
    id TEXT NOT NULL, version INTEGER NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    buffer_minutes INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
    attendees TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
    PRIMARY KEY (id, version));
"""


class BoundaryStore:
    """The protected commitment, versioned. Every edit is a new version, never an update.

    The version is what makes "the private constraint changed after you approved this" a
    detectable fact rather than a race: a change set binds to the boundary version it was built
    against, and a newer version makes it stale.
    """

    def __init__(self, conn: sqlite3.Connection | None = None, *, path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def put(self, *, id: str, start: str, end: str, buffer_minutes: int, title: str = "",
            location: str = "", reason: str = "", attendees=(), created_at: str = "") -> ProtectedCommitment:
        """Write the NEXT version of this commitment. Returns what was written."""
        import datetime as _dt
        if int(buffer_minutes) < 0:
            raise ValueError("buffer_minutes must not be negative")
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM protected_commitments WHERE id=?",
                (id,)).fetchone()
            version = int(row["v"]) + 1
            self._conn.execute(
                "INSERT INTO protected_commitments(id, version, start, end, buffer_minutes, "
                "title, location, reason, attendees, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (id, version, start, end, int(buffer_minutes), title, location, reason,
                 json.dumps(list(attendees)), created_at or _dt.datetime.now().isoformat(
                     timespec="seconds")))
            self._conn.commit()
        return ProtectedCommitment(id=id, start=start, end=end, buffer_minutes=int(buffer_minutes),
                                   version=version, title=title, location=location, reason=reason,
                                   attendees=tuple(attendees))

    def current(self, commitment_id: str) -> ProtectedCommitment | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM protected_commitments WHERE id=? ORDER BY version DESC LIMIT 1",
                (commitment_id,)).fetchone()
        return _from_row(row)

    def current_all(self) -> list[ProtectedCommitment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.* FROM protected_commitments c JOIN (SELECT id, MAX(version) AS v "
                "FROM protected_commitments GROUP BY id) m ON c.id = m.id AND c.version = m.v "
                "ORDER BY c.start").fetchall()
        return [c for c in (_from_row(r) for r in rows) if c is not None]

    def version_of(self, commitment_id: str) -> int:
        cur = self.current(commitment_id)
        return cur.version if cur else 0

    def planning_fact(self, commitment_id: str) -> dict:
        """The ONLY thing the planner receives. Four keys, and no way to spell a fifth."""
        cur = self.current(commitment_id)
        if cur is None:
            raise KeyError(f"no protected commitment '{commitment_id}'")
        return _fact(cur)

    def planning_facts(self) -> list[dict]:
        return [_fact(c) for c in self.current_all()]


def _fact(c: ProtectedCommitment) -> dict:
    return {"start": c.start, "end": c.end, "buffer_minutes": c.buffer_minutes, "label": LABEL}


def _from_row(row: sqlite3.Row | None) -> ProtectedCommitment | None:
    if row is None:
        return None
    try:
        attendees = tuple(json.loads(row["attendees"]))
    except (TypeError, ValueError):
        attendees = ()
    return ProtectedCommitment(id=row["id"], start=row["start"], end=row["end"],
                                buffer_minutes=int(row["buffer_minutes"]), version=int(row["version"]),
                                title=row["title"], location=row["location"], reason=row["reason"],
                                attendees=attendees)


# ---------------------------------------------------------------- the gate ---------------------
class PrivacyGate:
    """Refuses any outbound payload that carries — or paraphrases — a protected field.

    `audit` is optional and deliberately records that a refusal happened and which FIELD it
    concerned, never the value: a receipt that quoted the leak would be the leak.
    """

    #: Shorter values are not treated as markers: a two-word title would otherwise match half the
    #: English language and refuse everything. The fixture markers in the tests are long on
    #: purpose for the same reason.
    MIN_MARKER_LEN = 4

    def __init__(self, boundary: BoundaryStore, *, audit=None, overlap_ratio: float = 0.5,
                 overlap_min: int = 2, decisions=None, facts=None, care_circle=None) -> None:
        self.boundary = boundary
        self.audit = audit
        self.overlap_ratio = overlap_ratio
        self.overlap_min = overlap_min
        #: The decision log, if one is attached. Optional so a gate can be used in isolation; when
        #: present, every verdict this gate reaches is written down, refusals included.
        self.decisions = decisions
        #: The owner's protected facts, if this build has any. An object with `protected_values()`
        #: and `mark_used()` — `care.ProtectedFactStore` in practice — rather than the store type
        #: itself, so `boundary` keeps its one-way import direction and a test can hand in a stub.
        #: Absent means "no facts", which is the product before the care circle exists.
        self.facts = facts
        #: Who may receive what. Also duck-typed (`knows`, `may_receive`), for the same reason.
        self.care_circle = care_circle
        #: Disclosures the OWNER approved: {(artifact_id, field_name, bare_address)}. The one way a
        #: protected value may leave, and it is keyed on all three because approving "the digest"
        #: is not approving it to a different person, and approving it to a person is not approving
        #: a different fact. Empty by default — a gate with no approvals refuses everything, which
        #: is the direction that fails closed.
        self.approved_disclosures: set[tuple[str, str, str]] = set()

    # ---- the receipt --------------------------------------------------------------------------
    def _note(self, change_id: str, *, stage: str, source: str, subject: str, verdict: str,
              rule: str, detail: str | None = None) -> None:
        if self.decisions is None:
            return
        # No early return when `change_id` is empty. The classification call happens BEFORE a
        # change exists, so a refusal there has no change to hang from — and dropping it would lose
        # precisely the refusal that stopped the change from ever being read. It is filed under the
        # empty key, which `DecisionLog.orphans()` reads back.
        self.decisions.record(change_id or "", stage=stage, source=source, subject=subject,
                              verdict=verdict, rule=rule, detail=detail)

    def _refusal_subject(self, kind: str, field: str) -> str:
        """What the refusal was ABOUT, in the owner's words — never the value that was refused."""
        surface = {"model": "A prompt to the model", "search": "A web search",
                   "email": "The reply", "calendar": "A calendar field"}.get(
                       kind.split(":")[0], "An outbound payload")
        return f"{surface} ({field})" if field else surface

    # ---- what is protected --------------------------------------------------------------------
    def protected_values(self) -> list[tuple[str, str]]:
        """[(field name, value)] for every field that may never leave, across current versions.

        Two sources, and they are the same kind of object seen from two ends: the protected
        COMMITMENT (a meeting whose details are the owner's) and the protected FACTS (things about
        the person they care for). Both are values, both are compared the same way, and a refusal
        names the field either way — which is why they share one list rather than two checks. A
        second check would be a second thing to keep in step, and the failure of that is silent.
        """
        out: list[tuple[str, str]] = []
        for c in self.boundary.current_all():
            for name, value in (("title", c.title), ("location", c.location), ("reason", c.reason)):
                if isinstance(value, str) and len(_normalize(value)) >= self.MIN_MARKER_LEN:
                    out.append((name, value))
            for person in c.attendees:
                if isinstance(person, str) and len(_normalize(person)) >= self.MIN_MARKER_LEN:
                    out.append(("attendee", person))
        if self.facts is not None:
            for name, value in self.facts.protected_values():
                if isinstance(value, str) and len(_normalize(value)) >= self.MIN_MARKER_LEN:
                    out.append((str(name), value))
        return out

    def bind_care(self, *, facts=None, care_circle=None) -> "PrivacyGate":
        """Attach the protected facts and the care circle after construction.

        The same shape as `wiring.bind_decisions`, and for the same reason: the gate and the care
        stores are mutually dependent — the model needs the gate before the stores exist, and the
        stores need a gate to be checked by — so one of them is attached afterwards. Until it is,
        the gate has no facts and no circle, and both of those fail CLOSED (no fact is masked
        because none is known; no care recipient passes because there is no circle). The unattached
        state is the refusing one, which is what makes attaching it late safe.
        """
        if facts is not None:
            self.facts = facts
        if care_circle is not None:
            self.care_circle = care_circle
        return self

    # ---- what the model is allowed to see ------------------------------------------------------
    def scrub_for_model(self, messages):
        """Replace every protected fact's value with its placeholder before the model is handed it.

        The direction says a protected fact never enters a model prompt — the model sees a token,
        never the value — and there are two honest ways to get there: never build the prompt with
        it, or take it out. This is the second, and it is here rather than at each call site for
        the same reason `egress_sanitize` is in `llm.chat`: a new call path cannot forget a step it
        does not perform. `guard_model_payload` runs immediately afterwards and would refuse the
        payload if this had missed, so a masking bug is caught rather than hidden.

        Returns the messages unchanged when there are no facts, so the cost of this feature on a
        build that does not use it is one attribute test.
        """
        if self.facts is None:
            return messages
        masked, used = self.mask_for_model(messages)
        if used:
            self.facts.mark_used(used)
        return masked

    def mask_for_model(self, payload):
        """`scrub_for_model` without the side effect: the masked payload, and what was masked.

        Split out because a RECEIPT has to record the bytes the provider received, and recording
        them must not itself count as a use. `scrub_for_model` marks a fact used because the model
        really was shielded from it; a receipt written from the same value would otherwise be a
        second, invisible use, and `ProtectedFact.in_use` would say "needed today" about a fact that
        only a log ever touched. Both are the same masking call, which is the point — one
        implementation, two callers that differ only in whether they are reaching the provider.
        """
        if self.facts is None:
            return payload, []
        from .care import mask_payload

        return mask_payload(payload, self.facts.facts())

    def scrub_text(self, text: str) -> str:
        """One string, masked. For text on its way to a STORE or a RESPONSE rather than to a port.

        An inbound excerpt, a model's `request_excerpt`, a reason sentence: none of these is an
        outbound payload, so none of them passes the egress gate — and all of them are served back
        by `/today` and `/changes/{id}` and written to the audit database. Masking them at the
        point they enter the record is what makes "the value is in the prompt store" impossible
        rather than merely refused later. It is deliberately NOT applied to anything that is about
        to be sent: masking a draft before the gate saw it would turn a refusal into a message
        containing a placeholder.
        """
        if self.facts is None:
            return text
        masked, _ = self.mask_for_model(text)
        return masked

    def approve_disclosure(self, artifact_id: str, field_names, recipients) -> int:
        """Record that the OWNER approved these fields leaving, in THIS artifact, to THESE people.

        The only door in the gate that opens, and every part of the key is required. Approving the
        digest is not approving it to somebody else, approving it to somebody is not approving a
        different fact, and approving fact A is not approving fact B — a three-part key is what
        makes "the owner approved that exact artifact containing it, and the recipient is on the
        care circle" a checkable statement instead of a remembered one.

        Returns how many pairs were recorded, so a caller has a number to assert on rather than a
        silence to trust.

        MEMBERSHIP IS PART OF THE KEY. An approval names a person, and the list of people is the
        care circle — so a recipient who is not on the circle is not approvable, and neither is one
        on a build with no circle at all. Refused here rather than filtered out quietly: an
        approval recorded against an address nobody may send to is a key that opens nothing, and a
        caller that had to guess why would guess wrong.
        """
        circle = self.care_circle
        if circle is None:
            raise CareAudienceRefused(
                "refused: this build has no care circle, so there is nobody an approval may be "
                "recorded against. Add the people who may receive it first.")
        added = 0
        for name in {str(n) for n in field_names if n}:
            for address in {_address(a) for a in recipients or () if _address(a)}:
                if not circle.knows(address):
                    raise CareAudienceRefused(
                        "refused: a disclosure approval named an address that is not on your care "
                        "circle. Send only to people on your care circle.")
                self.approved_disclosures.add((str(artifact_id), name, address))
                added += 1
        return added

    def _disclosure_allowed(self, field: str, addresses) -> bool:
        """Whether EVERY recipient of this payload holds an approval for this field.

        Every, not any: a digest addressed to two people where one approved the fact is a
        disclosure to the other one. An empty recipient set is not a pass either — an artifact with
        no audience is not an artifact the owner approved to anybody, and reading it as one would
        open the door for exactly the payload shape nobody reviewed.

        And membership is re-asked HERE, at the moment of the check, rather than trusted from the
        moment of the approval. The circle is a list the owner edits, so "this address was a member
        when the approval was recorded" is not the same claim as "this address may receive it now" —
        a member who was removed between the two would otherwise keep a key that still fits.
        """
        bare = {_address(a) for a in addresses or () if _address(a)}
        if not bare:
            return False
        if self.care_circle is None:
            return False
        for addr in bare:
            if not self.care_circle.knows(addr):
                return False
        return all(any(key[1] == field and key[2] == addr for key in self.approved_disclosures)
                   for addr in bare)

    def planning_fact(self, commitment_id: str) -> dict:
        """Convenience: the allowed fact, reachable from the gate so callers hold one object."""
        return self.boundary.planning_fact(commitment_id)

    # ---- the checks ---------------------------------------------------------------------------
    def check_payload(self, payload, *, kind: str = "payload", change_id: str = "",
                      stage: str = "") -> None:
        """Scan every string in a nested structure. Raises on the first protected value found."""
        text = "\n".join(_iter_strings(payload))
        self.check_text(text, kind=kind, field="", change_id=change_id, stage=stage)

    def check_text(self, text: str, *, kind: str, field: str = "",
                   change_id: str = "", stage: str = "", artifact: str = "",
                   recipients=()) -> None:
        """`stage` overrides the one the kind would imply. The same surface is checked at more than
        one moment — an outbound reply is checked when it is composed and again at the port — and a
        refusal belongs on the rung where it happened. Empty means "ask the kind".

        `artifact` and `recipients` are what the ONE exception needs: a value the owner approved,
        in an artifact they approved, to people they approved. Both default to nothing, and
        `_disclosure_allowed` refuses on an empty audience, so a caller that never heard of this
        parameter gets the refusing behaviour rather than the permissive one. The check runs on the
        FINAL bytes of the payload — the caller passes the composed body, after every fragment has
        been rendered — because a check on the pieces is a check that a later composition step can
        walk around.
        """
        if not isinstance(text, str):
            raise EgressRefused(f"{kind}: {type(text).__name__} is not inspectable")
        # THE PAYLOAD, in every form it could be hiding a value in: percent, HTML, base64, a
        # Unicode fold, a homoglyph, and the raw bytes. The comparison below is against this
        # widened haystack, so "the value is not in the payload" is a claim about the payload
        # rather than about one spelling of it.
        flat = _payload_flat(text)
        # The payload's content tokens, once. Every value below is compared against the same
        # payload, so tokenizing it here rather than inside `_token_run` turns a per-value scan into
        # a per-payload one — same answer, and the cost stops scaling with how many facts the owner
        # has told the product about. Fetched only when there is a value to check, and cached across
        # the many `check_text` calls that scan the same email.
        values = self.protected_values()
        scan = _scan_of(flat) if values else None
        # One match per value, computed once and reused by the loop below. The two loops ask the
        # same question of the same value against the same payload — `_value_in` is
        # `bool(_how_present(...))`, rule for rule and in the same order — so computing it twice
        # made every scan of every field do its most expensive work in duplicate. The value pair
        # is carried alongside rather than a `{name: match}` dict because two protected facts may
        # share a name and a dict would quietly drop one of them.
        matches = [_how_present(value, flat, scan) for _name, value in values]
        for (name, value), match in zip(values, matches):
            if match:
                if artifact and self._disclosure_allowed(name, recipients):
                    # Approved, in this artifact, to exactly these people. Recorded as an ALLOWED
                    # disclosure and not skipped in silence: the owner approved a protected value
                    # leaving, and a spine that showed no row for it would be a spine with a hole
                    # in it at precisely the moment it matters most.
                    self._note(change_id, stage=stage or _stage_of(kind), source=_source_of(kind),
                               subject=self._refusal_subject(kind, name), verdict="allowed",
                               rule=DISCLOSURE_APPROVED_RULE,
                               detail=f"you approved disclosing the {name} in "
                                      f"{artifact} to {len({_address(a) for a in recipients})} "
                                      f"care-circle member(s)")
                    continue
                # WHICH REFUSAL, decided by HOW it was found. Verbatim is a protected leak; a
                # partial run, an encoding, or a token-shape match is a paraphrase — the value
                # left in a form that a marker test would have called absent. The two classes are
                # siblings and the owner's next move is different, so collapsing them into one
                # would make the row say "copied" about something that was reworded.
                self._refuse(ParaphraseLeak if match != "verbatim" else ProtectedLeak,
                             kind=kind, field=name,
                             extra=f" in {field}" if field else "",
                             detail="" if match == "verbatim" else f"matched {match}",
                             change_id=change_id, stage=stage, recipients=recipients)
        words = _content_words(flat)
        for (name, value), match in zip(values, matches):
            if match:
                # The value is present VERBATIM, and the loop above therefore either raised or
                # approved it — there is no third outcome. So the resemblance this branch is about
                # to measure is the approved occurrence itself, and refusing on it here would make
                # the owner's approval decorative: every value with enough content words to trip
                # this branch would be refused no matter what they approved. Skipping is what makes
                # "unless the owner approved that exact artifact" a checkable claim rather than a
                # sentence in a docstring.
                #
                # The rule the comment below states still holds, and this is not a hole in it: a
                # payload that MERELY resembles the fact — the value absent, the words shared —
                # arrives here with `_normalize(value) not in flat` and is refused as before. What
                # is skipped is a value the owner has already seen and approved in these bytes.
                continue
            protected_words = _content_words(_normalize(value))
            shared = words & protected_words
            if len(shared) >= self.overlap_min and len(shared) / max(1, len(protected_words)) >= (
                    self.overlap_ratio):
                # The refusal says HOW MUCH overlapped, never WHICH words. Naming them would turn
                # this into an oracle: a caller could probe candidate words and learn which ones
                # are in the protected reason, one refusal at a time.
                #
                # There is deliberately no approved-disclosure bypass on this branch. A disclosure
                # approval covers the owner handing over a fact they chose to hand over, and they
                # chose it as a VALUE — "send them the policy number". A payload that merely
                # resembles the fact is not a thing an approval can be about, because the owner
                # never saw the resemblance to approve it.
                self._refuse(ParaphraseLeak, kind=kind, field=name,
                             extra=f" in {field}" if field else "",
                             detail=f"{len(shared)} of {len(protected_words)} content words",
                             change_id=change_id, stage=stage, recipients=recipients)

    def guard_foreign_events(self, text: str, *, pinned: str, foreign, kind: str = "email",
                             field: str = "body", change_id: str = "", stage: str = "",
                             recipients=()) -> None:
        """Refuse a body that describes a meeting other than the one it is about.

        The engine answers one question — "move this meeting" — and the only calendar it may
        describe is the meeting in that question. Anything else on the day is a meeting nobody
        asked about, and the injection that asks for "my full schedule" is asking for exactly the
        thing this refuses. It refuses at COMPOSITION and again on the final bytes, from the same
        method, because those are the same question asked at two moments; `_refuse` records which
        moment it was.

        `foreign` is the day's other meetings reduced to fragments (see `foreign_fragments_of`),
        carried on the message rather than looked up here: the gate has no calendar and must not
        acquire one, and a check that re-read the day at send time would be answering about a day
        that may have moved since the owner approved the reply.

        The refusal names the EVENT — by id — and never the fragment that matched. `detail` carries
        the field the fragment came from, which is what an owner would go and look at, and the
        fragment itself never reaches the sentence, the log or the receipt."""
        if not pinned or not foreign:
            return
        found = foreign_event_in(text, foreign)
        if found is None:
            return
        named, how = found
        # The refusal says WHICH meeting, by id, and never the text that gave it away: a receipt
        # that quoted the fragment would be the disclosure it is reporting. The id is the owner's
        # own calendar's name for their own meeting, and the owner is who reads this row.
        subject = self._refusal_subject(kind, field)
        if self.audit is not None:
            self.audit.record("egress.foreign_event_refused",
                              detail={"kind": kind, "field": field, "event": named,
                                      "rule": ForeignEventLeak.__name__})
        self._note(change_id, stage=stage or _stage_of(kind), source=_source_of(kind),
                   subject=subject, verdict="refused", rule=RULE_CALENDAR_NOTE,
                   detail=(f"it described {named or 'another meeting'}, which is not the meeting "
                           f"this change is about (matched a {how} form of its details)"))
        refusal = ForeignEventLeak(
            f"refused: the outbound {kind} payload describes {named or 'another meeting'}, which "
            f"is not the meeting this change is about. A reply states the meeting it was asked "
            f"about and nothing else on the day.")
        refusal.kind, refusal.field = kind, field
        raise refusal

    def note_foreign_fragment_refused(self, *, source: str, event: str, how: str,
                                      field: str = "body", change_id: str = "") -> None:
        """Write down that one piece of the reply was dropped, and which meeting it described.

        The receipt for a DROP rather than for a message that never left, which is why it is not
        `_refuse`: nothing was refused to the owner — their meeting is still being moved — and only
        the piece that described somebody else's meeting is gone. The row names the source it
        claimed and the event it would have revealed, by id, and never its text: the whole reason
        it is being dropped is that the text says something it must not."""
        subject = self._refusal_subject("email", field)
        if self.audit is not None:
            self.audit.record("egress.foreign_fragment_dropped",
                              detail={"field": field, "source": source, "event": event,
                                      "rule": ForeignEventLeak.__name__})
        self._note(change_id, stage="validate", source=_source_of("email"), subject=subject,
                   verdict="refused", rule=RULE_CALENDAR_NOTE,
                   detail=(f"a piece of the reply that claimed to come from the {source or 'draft'} "
                           f"was dropped: it described {event or 'another meeting'} "
                           f"(matched a {how} form of its details), and your reply is about the "
                           f"one meeting this change names"))

    def note_ineligible_fragment(self, *, source: str, change_id: str = "", pinned: str = "") -> None:
        """Write down that a piece of the reply was not the engine's, and so never became bytes.

        The sibling of `note_foreign_fragment_refused`, and the difference between the two is the
        whole fourth review. That one fires when a piece MATCHED another meeting, which is a
        question the attacker gets to answer by choosing a spelling. This one fires when a piece
        was never eligible in the first place — the engine did not render it, so there is nothing
        to match and nothing to spell. The row names the source it claimed and never its text,
        for the reason the other one does: the text is what must not travel, and a receipt that
        quoted it would be the disclosure it is reporting."""
        subject = self._refusal_subject("email", "body")
        if self.audit is not None:
            self.audit.record("egress.ineligible_fragment_dropped",
                              detail={"source": source, "pinned": pinned,
                                      "rule": SchemaLeak.__name__})
        self._note(change_id, stage="validate", source=_source_of("email"), subject=subject,
                   verdict="refused", rule=RULE_CALENDAR_NOTE,
                   detail=(f"a piece of the reply that claimed to come from the "
                           f"{source or 'draft'} was dropped: the engine did not write that "
                           f"sentence, so it could not become part of your reply"))

    # ---- who may receive ----------------------------------------------------------------------
    def guard_recipients(self, to, *, permitted, change_id: str = "", stage: str = "send") -> None:
        """Every recipient must be one the caller declared. Enforced, not assumed.

        `permitted` is required and has no default. An outbound email whose permitted audience was
        never stated is an email nobody authorised, and a guard that defaults to "allow" is the
        guard this module exists to not be. The comparison is on the bare address: a display name
        is chosen by whoever sent the message, so `Alex Doe <evil@example.com>` must not pass a
        check that `Alex Doe <alex@example.com>` was meant to satisfy.
        """
        allowed = {_address(a) for a in permitted or ()}
        for field_name in RECIPIENT_FIELDS:
            if field_name not in ("to",):
                # No cc and no bcc exist on this path. If one ever appears it arrives here and is
                # refused by name, rather than travelling because nothing looked.
                continue
            for addr in to or ():
                if _address(addr) not in allowed:
                    self._note(change_id, stage=stage, source=_source_of("email:recipients"),
                               subject="The reply's recipients", verdict="refused",
                               rule=RECIPIENT_RULE,
                               detail=f"a recipient outside the thread: {len(allowed)} permitted")
                    if self.audit is not None:
                        self.audit.record("egress.recipient_refused",
                                          detail={"kind": "email:recipients",
                                                  "permitted": len(allowed)})
                    raise RecipientRefused(
                        "refused: the reply names a recipient the thread it answers did not come "
                        "from. A reply goes only to the person who wrote to you.")
        self._note(change_id, stage=stage, source=_source_of("email:recipients"),
                   subject="The reply's recipients",
                   verdict="allowed", rule=RECIPIENT_RULE,
                   detail=f"{len(tuple(to or ()))} recipient(s), all from the thread")

    def guard_care_recipients(self, to, *, kind: str, change_id: str = "",
                              stage: str = "send", snapshot=None) -> None:
        """Every recipient of a care action must be on the circle, for THIS kind of action.

        Two questions, asked separately because they fail differently. Not on the circle at all is
        "who is this?"; on the circle but not granted this kind is "you did not give them this".
        Both refuse, and the refusal says which, because the owner's next move is different: one is
        a name they do not recognise, the other is a permission they can grant in one tap.

        This is where clause 7 is ENFORCED. The clause in `limits.py` is the sentence the owner
        reads; this is the check that makes it true, and it is in the gate rather than in the
        engine's limit review so that an action which never went through a limit review — a digest,
        a future care action — still cannot reach a stranger.

        Both questions are answered from ONE snapshot. Reading `knows` and then `may_receive` is two
        reads of a list the owner can edit between them, and the window is not theoretical: a member
        revoked in that window is a member this check would have cleared, and a member ADDED in it
        is one the check would have refused. `snapshot` lets a caller pin the reading at
        COMPOSITION and re-ask it at SEND, which is the only way "the circle as it stood when the
        owner saw this" is a checkable statement — see `care.CircleSnapshot`.
        """
        circle = snapshot if snapshot is not None else self.care_circle
        if circle is None:
            # A build with no circle has no one it may send care actions to. Refusing every
            # recipient is the fail-closed reading, and the alternative — treating "no circle" as
            # "no restriction" — is a missing feature turning into an open door.
            raise CareAudienceRefused(
                "refused: this build has no care circle, so there is nobody a care action may be "
                "sent to. Add the people who may receive it first.")
        for addr in to or ():
            bare = _address(addr)
            if not circle.knows(bare):
                self._note(change_id, stage=stage, source=_source_of("email:recipients"),
                           subject="A care action's recipients", verdict="refused",
                           rule=CARE_CIRCLE_RULE,
                           detail=f"an address outside your care circle was named "
                                  f"({len(circle.addresses())} member(s))")
                if self.audit is not None:
                    self.audit.record("egress.care_audience_refused",
                                      detail={"kind": kind, "why": "not_a_member"})
                raise CareAudienceRefused(
                    "refused: a care action named an address that is not on your care circle. "
                    "Send only to people on your care circle.")
            if not circle.may_receive(bare, kind):
                self._note(change_id, stage=stage, source=_source_of("email:recipients"),
                           subject="A care action's recipients", verdict="refused",
                           rule=CARE_CIRCLE_RULE,
                           detail=f"a member was named for a kind they were not given ({kind})")
                if self.audit is not None:
                    self.audit.record("egress.care_audience_refused",
                                      detail={"kind": kind, "why": "not_granted"})
                raise CareAudienceRefused(
                    f"refused: a member of your care circle was named for '{kind}', which you did "
                    f"not list them for. Send only what you listed each person for.")
        self._note(change_id, stage=stage, source=_source_of("email:recipients"),
                   subject="A care action's recipients", verdict="allowed", rule=CARE_CIRCLE_RULE,
                   detail=f"{len(tuple(to or ()))} recipient(s), all on your care circle")

    # ---- the surfaces -------------------------------------------------------------------------
    def guard_model_payload(self, messages, *, task_id: str = "", phase: str = "") -> None:
        """Every model call, including intent, extraction, research planning and the answer."""
        self.check_payload(messages, kind=f"model:{phase or 'unlabelled'}", change_id=task_id)
        self._note(task_id, stage="model", source=_source_of("model"),
                   subject=f"A prompt to the model ({phase or 'unlabelled'})", verdict="allowed",
                   rule=RULE_MODEL_NOTE,
                   detail="checked for anything you keep private before it was sent")

    def guard_search_query(self, query: str, *, include_domains=(), change_id: str = "") -> None:
        self.check_text(query, kind="search", change_id=change_id)
        self.check_payload(list(include_domains), kind="search:domains", change_id=change_id)
        self._note(change_id, stage="search", source=_source_of("search"),
                   subject=f"Search: {_plain_query(query)}", verdict="allowed",
                   rule=RULE_SEARCH_NOTE, detail="the query carries nothing you keep private")

    def guard_calendar_fields(self, change_id: str = "", stage: str = "execute", *,
                              pinned: str = "", foreign=(), **fields) -> None:
        """Every calendar field that can be written, including the human-readable ones a leak
        would actually travel in.

        `stage` names the MOMENT and not the surface, for the same reason `guard_email` takes one:
        this check runs twice — once while the venue is still a proposal nobody has approved, and
        once on the arguments the port is about to write — and recording both as `execute` puts an
        `execute` row above the `approve` row. A spine rendered in order then shows the owner a
        calendar write that happened before they said yes.

        `pinned` and `foreign` are the same pair `guard_foreign_events` takes and are optional for
        the same reason it takes them: a caller that has no day in hand still gets the protected
        value check, and a caller that has one gets the second question asked as well — see
        `guard_calendar_foreign` for why the calendar needs both.
        """
        self.check_payload(fields, kind="calendar", change_id=change_id)
        self.guard_calendar_foreign(fields, pinned=pinned, foreign=foreign,
                                    change_id=change_id, stage=stage)
        if not change_id:
            # A bundle that does not say which change it belongs to is checked and refused exactly
            # the same, but the ALLOWED row has no spine to sit on. Filing it in the decision log
            # anyway would put it in `orphans()` — the bucket the harness reads as "a decision that
            # got away from the change it belongs to" — so it goes to the audit instead, which is
            # the record that does not require a change to exist.
            if self.audit is not None:
                self.audit.record("egress.calendar_checked",
                                  detail={"fields": sorted(fields)})
            return
        self._note(change_id, stage=stage, source=_source_of("calendar"),
                   subject="A calendar field", verdict="allowed", rule=RULE_CALENDAR_NOTE,
                   detail="checked for anything you keep private before it was written")

    def guard_calendar_foreign(self, fields: dict, *, pinned: str, foreign, change_id: str = "",
                               stage: str = "execute") -> None:
        """Refuse a calendar field that describes a meeting other than the one being written.

        The same question `guard_foreign_events` asks of a reply, asked of the write — and it has to
        be asked separately, because the two surfaces do not share a payload. The check that was
        here read `fields` for PROTECTED VALUES and nothing else: a venue is a place, the place the
        gate protects is the owner's own protected commitment's, and ANOTHER MEETING'S location is
        neither. So a model that chose a foreign place as the venue wrote it to the pinned event and
        the calendar update carried `send_updates="all"` — a notification to the attendees of the
        meeting whose address just went out. The value check cannot see that, because the value is
        not protected; it is somebody else's, which is a different question with a different answer.

        Read over every field the caller passed EXCEPT the ones the engine scheduled — see
        `_CALENDAR_ENGINE_FIELDS`, which is an exclusion list so that a field added later is covered
        by default rather than covered when somebody remembers. The values are walked as payloads
        rather than as strings, so an attendee list is read as its addresses and a nested shape is
        read at all: `foreign_event_in` decodes what it is given, the same reader the reply uses.

        The refusal names the EVENT and the FIELD, never the fragment that matched — the fragment is
        the address that must not go out, and a receipt quoting it would be the disclosure."""
        if not pinned or not foreign:
            return
        for name in sorted(fields):
            if name in _CALENDAR_ENGINE_FIELDS:
                continue
            found = foreign_event_in(fields[name], foreign)
            if found is None:
                continue
            named, how = found
            who = named or "another meeting"
            if self.audit is not None:
                self.audit.record("egress.foreign_event_refused",
                                  detail={"kind": "calendar", "field": name, "event": named,
                                          "rule": ForeignEventLeak.__name__})
            self._note(change_id, stage=stage, source=_source_of("calendar"),
                       subject=self._refusal_subject("calendar", name), verdict="refused",
                       rule=RULE_CALENDAR_NOTE,
                       detail=(f"the {name} it would have written describes {who}, which is not "
                               f"the meeting this change is about "
                               f"(matched a {how} form of its details)"))
            raise ForeignEventLeak(
                f"refused: the {name} on this calendar write describes {who}, which is not the "
                f"meeting this change is about. A calendar write states the meeting it was asked "
                f"about and nothing else on the day.")

    def guard_email(self, email: OutboundEmail, *, change_id: str = "", permitted=None,
                    stage: str = "send", artifact: str = "", care_kind: str = "") -> None:
        """An outbound email is checked three times: its schema, its text, and its audience.

        `stage` names the MOMENT, not the surface, because this check runs at two of them. At
        compose time the reply is a proposal nobody has approved yet; at the port it is the message
        that is about to leave. Recording both as `send` puts a `send` row three rows above the
        `approve` row, and a UI rendering the spine in order then shows the owner a reply that went
        out before they said yes. The stage is a parameter for exactly that reason.
        """
        payload = email.as_payload()
        extra = set(payload) - EMAIL_FIELDS
        if extra:
            raise SchemaLeak(f"outbound email has field(s) outside the allowlist: "
                             f"{', '.join(sorted(extra))}")
        # A cc/bcc that is not on `OutboundEmail` cannot be reached by `as_payload`, so the check
        # above can never see one. This is the check that would: the class is a dataclass and a
        # later field would be carried here without anyone editing this method.
        for name in RECIPIENT_FIELDS - {"to"}:
            if getattr(email, name, None):
                self._note(change_id, stage=stage, source=_source_of("email:recipients"),
                           subject="The reply", verdict="refused", rule=RECIPIENT_RULE,
                           detail=f"the email carries a {name}")
                raise RecipientRefused(
                    f"refused: the outbound email carries a {name}. Only 'to' exists on this "
                    f"path; a {name} is a recipient nobody reviewed.")
        if email.body != email.rendered():
            # The body is not the render of its own declared fragments: someone handed over prose.
            raise SchemaLeak(
                "outbound email body is not the render of its allowlisted fragments — free-form "
                "text is not permitted on the egress path")
        # THE SUBJECT IS HELD TO THE SAME RULE, and this is the sixth review's first finding. The
        # body equality above has existed since the fifth review and the subject sat beside it with
        # only a protected-value scan for company — so a subject nobody rendered reached the
        # provider's `Subject` header, which is a line the recipient reads and, in a thread, the
        # line every client files the conversation under. The scan could not catch it either: a
        # novel subject is not a protected value, it is simply untrue, and "is this the render of
        # the pieces the engine declared" is a different question from "does this disclose
        # something the owner keeps private".
        #
        # Asked of the BYTES, exactly like the body, so a caller that built the object directly is
        # refused by the same rule rather than by a second, thinner one.
        if email.subject_template and email.subject_template not in SUBJECT_TEMPLATES:
            raise SchemaLeak(
                f"outbound email subject template {email.subject_template!r} is not one of "
                f"{', '.join(sorted(SUBJECT_TEMPLATES))}")
        _check_fragment_sources(email.subject_fragments, what="subject fragment")
        if email.subject != email.subject_rendered():
            raise SchemaLeak(
                "outbound email subject is not the render of its own declared pieces — a subject "
                "is the thread's own words behind the engine's prefix, and a subject that no "
                "source declared is not permitted on the egress path")
        if not email.subject_rendered():
            # "Nothing was declared" and "the render of nothing" are the same empty string, and an
            # empty subject is not a subject. Refused rather than sent: a message with no subject
            # is one whose header nobody composed, which is the state this check exists for.
            raise SchemaLeak(
                "outbound email has no rendered subject — every message this build sends says "
                "which thread it answers or which day it is about")
        # Re-asked here, and not only in `compose_email`: the class is a dataclass and a caller that
        # built one directly would otherwise carry a template nobody source-checked. The composer is
        # one door into this object and `OutboundEmail(...)` is another.
        if email.template_body and email.template_source not in ALLOWED_FRAGMENT_SOURCES:
            raise SchemaLeak(
                f"outbound email template source {email.template_source!r} is not on the outbound "
                f"allowlist — a fixed sentence still has to come from somewhere this build listed")
        self.check_text(email.subject, kind="email", field="subject", change_id=change_id,
                        stage=stage, artifact=artifact, recipients=email.to)
        self.check_text(email.body, kind="email", field="body", change_id=change_id, stage=stage,
                        artifact=artifact, recipients=email.to)
        # The subject is checked too, and by the same method: it is `Re: <the thread's subject>`
        # on this path and so carries nothing, but the check is on the BYTES rather than on the
        # expectation that they are boring — a subject is a header a recipient sees, and "the
        # builder happens to derive it" is a fact about today's builder, not about this message.
        self.guard_foreign_events(
            f"{email.subject}\n{email.body}", pinned=email.pinned_event,
            foreign=getattr(email, "foreign_fragments", ()), kind="email", field="body",
            change_id=change_id, stage=stage, recipients=email.to)
        self.check_payload(list(email.to), kind="email:recipients", change_id=change_id,
                           stage=stage)
        if permitted is not None:
            self.guard_recipients(email.to, permitted=permitted, change_id=change_id, stage=stage)
        if care_kind:
            # The audience question for a CARE action, which is a different list from the thread's.
            # Both are asked when a caller declares both, and neither is inferred from the other:
            # a reply that also happens to be a care action must satisfy the thread rule AND the
            # circle, and a check that picked one would be a check an attacker picks.
            # Against the snapshot the email was COMPOSED with when it carries one, so a care action
            # is checked against the circle the owner's screen was showing rather than against a
            # list that may have moved underneath it. See `guard_care_recipients`.
            self.guard_care_recipients(email.to, kind=care_kind, change_id=change_id, stage=stage,
                                       snapshot=getattr(email, "care_snapshot", None))
        self._note(change_id, stage=stage, source=_source_of("email"),
                   subject=("The reply, as composed" if stage != "send" else "The reply to send"),
                   verdict="allowed", rule=RULE_SEND_NOTE,
                   detail=("checked for anything you keep private before it was sent"
                           if stage == "send" else
                           "composed only from reviewed pieces, and checked"))

    def _refuse(self, exc, *, kind: str, field: str, extra: str = "",
                detail: str = "", change_id: str = "", stage: str = "",
                recipients=()) -> None:
        if self.audit is not None:
            # The field and the rule, plus a measurement where one is meaningful — never the value
            # that was refused, and never a fragment of it.
            self.audit.record("egress.protected_refused",
                              detail={"kind": kind, "field": field, "rule": exc.__name__,
                                      **({"overlap": detail} if detail else {})})
        # WHO it would have gone to, as a COUNT and never as an address. The direction asks for
        # "would have disclosed <field> to <recipient>" without echoing the value, and the count is
        # the half of that a decision row can carry safely: an address in a receipt is a recipient
        # list in a log, and a log is a thing that gets read, exported and pasted into a bug report.
        # The number is enough for the owner to know whether a disclosure would have been internal
        # to their circle or out of it.
        audience = len({_address(a) for a in recipients or () if _address(a)})
        self._note(change_id, stage=stage or _stage_of(kind), source=_source_of(kind),
                   subject=self._refusal_subject(kind, field), verdict="refused",
                   rule=_rule_of(kind),
                   detail=(f"the protected {field} would have been disclosed"
                           + (f" to {audience} recipient(s)" if audience else "")
                           + (f" ({detail})" if detail else "")))
        refusal = exc(f"refused: the outbound {kind} payload discloses the protected {field}{extra}"
                      + (f" ({detail})" if detail else ""))
        # Set on the INSTANCE, so two refusals of different fields are two different exceptions
        # even when they are the same class. See `EgressRefused` for why these exist at all.
        refusal.kind, refusal.field = kind, field
        raise refusal


class GuardedSearch:
    """A SearchPort that cannot be asked a question about the protected commitment.

    Wrapping the port rather than checking at the call site is the point: the engine holds this
    object, so there is no `search()` to reach that skips the gate.
    """

    def __init__(self, port, gate: PrivacyGate) -> None:
        self.port = port
        self.gate = gate
        self.queries: list[str] = []
        #: Whether the results that come back through here are a simulator's. Read off the wrapped
        #: port and re-exposed rather than looked up through `.port` at the call site, so the
        #: engine asks the object it holds and a later wrapper cannot lose the answer.
        self.is_sample = bool(getattr(port, "is_sample", False))

    def search(self, query: str, *, max_results: int = 5,
               include_domains: tuple[str, ...] = (), change_id: str = "") -> list[Evidence]:
        self.gate.guard_search_query(query, include_domains=include_domains, change_id=change_id)
        self.queries.append(query)
        return self.port.search(query, max_results=max_results, include_domains=include_domains)


# ---------------------------------------------------------------- text helpers ----------------
def _normalize(text: str) -> str:
    return _NORMALIZE_RE.sub(" ", str(text).casefold()).strip()


def _content_words(flat: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(flat) if t not in _STOPWORDS}


def _token_list(flat: str) -> list[str]:
    """The content tokens of a normalized string, IN ORDER. `_content_words` answers "which words",
    this answers "which words, next to each other" — and the second is what tells a shared
    vocabulary from the same value shortened."""
    return [t for t in _TOKEN_RE.findall(flat) if t not in _STOPWORDS]


#: Shared empty answer for `_token_run`'s lookups, so a token the payload does not contain costs a
#: dict miss rather than building a fresh empty container.
_NO_POSITIONS: frozenset = frozenset()


@lru_cache(maxsize=8)
def _scan_of(flat: str) -> tuple[tuple[str, ...], dict]:
    """The payload's content tokens and, for each, WHERE it occurs — computed once per payload.

    A single email is scanned by many `check_text` calls — one per field, one per stage, one per
    composed fragment — and every one of them asks the same two questions of the same 2000
    characters: which words are in it, and where. Both answers were being rebuilt per value per
    call, which is what made the widened gate expensive: a few hundred positions, reconstructed
    several hundred times to answer questions about a string that never changed.

    Indexed BY TOKEN rather than as one flat set of `(token, index)` pairs, because the caller's
    question is always "where does THIS word occur": the flat set made every start token walk the
    whole payload to find its own occurrences, while this walks only those. Tokens are a tuple and
    each position set a frozenset so a caller cannot edit a shared answer.
    """
    tokens = tuple(_token_list(flat))
    found: dict[str, list[int]] = {}
    for i, token in enumerate(tokens):
        found.setdefault(token, []).append(i)
    return tokens, {token: frozenset(where) for token, where in found.items()}


@lru_cache(maxsize=256)
def _value_tokens(value_flat: str) -> tuple[str, ...]:
    """`_token_list` of a protected VALUE, cached. Values are the stable half of the comparison —
    the owner's facts do not change between the forty-five scans of one email — so the same short
    token list was being rebuilt for every payload it was checked against."""
    return tuple(_token_list(value_flat))


def _decoded_forms(text) -> list[str]:
    """The same text in every representation a value could be hiding inside.

    The three the direction names — percent, HTML, base64 — plus the Unicode ones: an NFKD fold,
    which turns a full-width or mathematical-alphanumeric spelling back into ASCII, and a homoglyph
    fold for the Cyrillic and Greek twins. Every one of them is applied to the PAYLOAD and the
    result compared to the value, which is the direction that cannot be walked around by inventing
    an encoding this file has not heard of: an encoding the engine does not decode is one that
    reaches the reader as itself, and a benefits id spelled in Cyrillic is not a benefits id to
    anybody except the person who typed it.
    """
    raw = str(text)
    out = [raw]
    folded = raw.translate(_HOMOGLYPHS)
    if folded != raw:
        out.append(folded)
    try:
        out.append(unicodedata.normalize("NFKD", folded))
    except (TypeError, ValueError):                                   # pragma: no cover - defensive
        pass
    try:
        unquoted = urllib.parse.unquote_plus(folded)
        if unquoted != folded:
            out.append(unquoted)
    except (TypeError, ValueError):                                   # pragma: no cover - defensive
        pass
    try:
        unescaped = html.unescape(folded)
        if unescaped != folded:
            out.append(unescaped)
    except (TypeError, ValueError):                                   # pragma: no cover - defensive
        pass
    for run in _B64_RUN_RE.findall(folded):
        try:
            blob = base64.b64decode(run + "=" * (-len(run) % 4), validate=True)
        except (ValueError, TypeError):
            continue
        try:
            decoded = blob.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if decoded.isprintable():
            out.append(decoded)
    return out


def _payload_flat(text) -> str:
    """Every decoded form of a payload, normalized and joined. The haystack `_how_present` searches."""
    return "\n".join(_normalize(form) for form in _decoded_forms(text))


# ---------------------------------------------------------------- what identifies a meeting -----
#: The shortest word that can identify a meeting. Below this a "title" is a syllable that matches
#: half the language, and a check that refuses on it is a check somebody turns off — the same
#: reasoning, and the same number, as `PrivacyGate.MIN_MARKER_LEN`.
MIN_FRAGMENT_LEN = 4

#: `H:MM` / `H.MM`, optionally with a meridiem. The `(?<![\d+-])` is what keeps a UTC offset out of
#: it: `+01:00` in an ISO timestamp is a timezone, not an appointment, and reading it as one would
#: make every event in `Europe/London` look like it started at one in the morning.
_CLOCK_RE = re.compile(r"(?<![\d+\-])(\d{1,2})[:.](\d{2})\s*(am|pm|a\.m\.|p\.m\.)?(?!\d)",
                       re.IGNORECASE)
#: `9am`, `0915am` — a meridiem is required, because without one a bare number is a number. The
#: lookbehind also excludes `:` and `.`, and that exclusion is load-bearing rather than tidy:
#: without it the `15` of `9.15am` is read as fifteen-hundred hours, and a reply about a quarter
#: past nine is refused for naming a meeting at three in the afternoon.
_MERIDIEM_RE = re.compile(r"(?<![\d+\-:.])(\d{1,4})\s*(am|pm|a\.m\.|p\.m\.)(?![\w])", re.IGNORECASE)
#: `9 15am`, `9 15 am` — the same clock with the separator taken out and a space left where it was.
#: The third of the three separators a numeric time is written with, and the one the first version
#: of this reader did not know: it read the `15am` inside `9 15am` as three in the afternoon, so the
#: text named 09:15 and the reader reported 15:00 — a miss and an invention from the same four
#: characters. A meridiem is REQUIRED, for `_MERIDIEM_RE`'s reason: without one `9 15` is two
#: numbers, and a reader that takes every pair of numbers for a clock refuses a reply about "3 15
#: people". The lookbehind is `_CLOCK_RE`'s — an offset is not an appointment — with `-` added so a
#: hyphenated range is not one either.
_SPACED_CLOCK_RE = re.compile(
    r"(?<![\d:.\-+])(\d{1,2})[ \t]+(\d{2})\s*(am|pm|a\.m\.|p\.m\.)(?![\w])", re.IGNORECASE)
_WORD_CLOCK_RE = re.compile(
    # The direction words are the half of this that the first version enumerated rather than read.
    # `past` and `after` are the same instruction and so are `to` and `before`; a reader that knows
    # one spelling of each is a reader a model walks around by typing the other, which is exactly
    # what "a quarter AFTER nine" did to the version of this file that the round-3 review measured.
    # The minutes are listed longest-first because an alternation takes the first branch that
    # matches, and "twenty" would otherwise win against "twenty five" and report 09:05 for 09:25.
    r"\b(?:(?P<min>twenty[\s-]?five|quarter|half|twenty|ten|five)\s+"
    r"(?P<dir>past|after|to|before)\s+(?P<hour1>"
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    r"|(?P<hour2>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+o'clock"
    # `nine fifteen`, and `nine-fifteen`. The fourth spelling, and the one an evader reaches for
    # first: it is an explicit clock, it is how a person writes a time in a sentence, and a reader
    # that knows only the three canonical forms is a reader a model can walk around. The minutes
    # are listed longest-first for the reason above.
    r"|(?P<hour3>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)[\s-]+"
    r"(?P<min3>twenty[\s-]?five|thirty[\s-]?five|forty[\s-]?five|fifty[\s-]?five|fifteen|"
    r"thirty|forty|fifty|twenty|ten|five))\b",
    re.IGNORECASE)
_HOUR_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
               "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_MINUTE_WORDS = {"five": 5, "ten": 10, "quarter": 15, "twenty": 20, "half": 30, "twenty five": 25}
#: The minutes in the `nine fifteen` form, which are said rather than counted past or to an hour.
_SPOKEN_MINUTES = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25,
                   "thirty": 30, "thirty five": 35, "forty": 40, "forty five": 45, "fifty": 50,
                   "fifty five": 55}
_NAMED_CLOCKS = {"noon": 12 * 60, "midday": 12 * 60, "midnight": 0}

#: An ISO timestamp's own `HH:MM`, in the spelling a fragment is compared in.
_ISO_CLOCK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ](\d{2}):(\d{2})")


def clock_of(value: str) -> str:
    """`"HH:MM"` when a value IS a timestamp, and `""` when it is not.

    The one place that decides what a meeting's start time looks like when it is written down
    rather than quoted, so the engine's check and the harness's check compare the same string."""
    match = _ISO_CLOCK_RE.match(str(value or "").strip())
    return f"{match.group(1)}:{match.group(2)}" if match else ""


def minutes_of(clock: str) -> int:
    """`"09:15"` as minutes since midnight, or `-1` when the string is not a canonical clock."""
    match = re.fullmatch(r"(\d{2}):(\d{2})", str(clock or ""))
    return int(match.group(1)) * 60 + int(match.group(2)) if match else -1


def _clock_candidates(flat: str) -> list[tuple[tuple[int, int], int]]:
    """Every clock spelling in `flat` as `((start, end), minutes)`, most specific spelling first.

    The order is the whole of the overlap rule the caller applies to it. `9 15am` is one clock, and
    the `15am` sitting inside it is another reading of the same four characters that says three in
    the afternoon; a reader that reports both has answered one piece of text twice with two
    different times, and the answer it invented is the one that refuses a correct reply. Listing
    the longer, more specific spelling before the shorter one it contains is what makes "first
    reading of a span wins" a rule rather than a coin toss.

    A spelling whose hour or minute is out of range is dropped rather than clamped: `99:99` is not a
    time somebody wrote, and turning it into one would put a meeting in the refusal set that the
    text never named."""
    out: list[tuple[tuple[int, int], int]] = []
    for match in _SPACED_CLOCK_RE.finditer(flat):
        hour = _to_24(int(match.group(1)), match.group(3))
        out.append((match.span(), hour * 60 + int(match.group(2))))
    for match in _CLOCK_RE.finditer(flat):
        hour = int(match.group(1))
        if match.group(3):
            hour = _to_24(hour, match.group(3))
        out.append((match.span(), hour * 60 + int(match.group(2))))
    for match in _MERIDIEM_RE.finditer(flat):
        digits = match.group(1)
        # `915am` and `0915am` are the same time written without the colon; `9am` is on the hour.
        hour, minute = ((int(digits[:-2]), int(digits[-2:])) if len(digits) > 2
                        else (int(digits), 0))
        out.append((match.span(), _to_24(hour, match.group(2)) * 60 + minute))
    for match in _WORD_CLOCK_RE.finditer(flat):
        if match.group("hour2"):
            out.append((match.span(), _HOUR_WORDS[match.group("hour2").casefold()] % 12 * 60))
            continue
        if match.group("hour3"):
            # `twelve fifteen` is a quarter past noon on a twelve-hour clock and 12:15 on a
            # twenty-four hour one; midnight is 00:15. The reader takes the twelve-hour reading
            # because that is the only one the spelling carries, and 12:15 is both.
            hour = _HOUR_WORDS[match.group("hour3").casefold()] % 12
            said = re.sub(r"[\s-]+", " ", match.group("min3").casefold())
            out.append((match.span(), hour * 60 + _SPOKEN_MINUTES[said]))
            continue
        hour = _HOUR_WORDS[match.group("hour1").casefold()]
        minute = _MINUTE_WORDS[re.sub(r"[\s-]+", " ", match.group("min").casefold())]
        # `to` and `before` count BACKWARDS from the hour — "a quarter to nine" and "a quarter
        # before nine" are both 08:45 — and `past` and `after` count forwards from it. The four are
        # two instructions written four ways, and a reader that knew only one spelling of each is
        # the reader "a quarter after nine" walked around.
        if match.group("dir").casefold() in ("to", "before"):
            hour, minute = (hour - 1) or 12, 60 - minute
        out.append((match.span(), hour % 12 * 60 + minute))
    return out


def time_of_day_mentions(text: str) -> set[int]:
    """Every time of day a piece of text NAMES, as minutes since midnight — in any spelling.

    A reply does not have to quote `2026-09-19T09:15:00+01:00` to say when somebody's standup is.
    It says "09:15", or "9.15am", or "9 15am", or "quarter past nine", or "a quarter after nine",
    and a check that knows only some of those spellings is a check that reports green about the
    rest. This is the reader for all of them — the FORMS rather than a list of the phrasings
    somebody thought of, because an enumeration is a thing the next phrasing walks around, and the
    round-3 review measured exactly that: "a quarter after nine" passed a reader that knew "past".

    It is deliberately conservative about what counts: a meridiem, a `:`, a `.`, a space between
    two numbers, or an explicit clock word. A bare number is a number — reading "3" as three
    o'clock would refuse every reply that mentions a count, and a check that fires on the product
    working is one people switch off. What that leaves outside the reader is written down in
    `docs/THREAT_MODEL.md` rather than claimed here: a paraphrase that carries no clock at all
    ("late morning", "after lunch") names a time this cannot read, and saying otherwise would be
    the kind of green this file exists to not hand out."""
    out: set[int] = set()
    flat = str(text or "")
    kept: list[tuple[int, int]] = []
    for span, minutes in _clock_candidates(flat):
        if not (0 <= minutes // 60 <= 23 and 0 <= minutes % 60 <= 59):
            continue
        if any(span[0] < end and start < span[1] for start, end in kept):
            continue
        kept.append(span)
        out.add(minutes)
    for word, minutes in _NAMED_CLOCKS.items():
        if re.search(rf"\b{word}\b", flat, re.IGNORECASE):
            out.add(minutes)
    return out


def _to_24(hour: int, meridiem: str) -> int:
    """A 12-hour reading with its meridiem, as a 24-hour hour. `12am` is midnight, not noon."""
    pm = meridiem.casefold().startswith("p")
    if hour == 12:
        return 12 if pm else 0
    return hour + 12 if pm else hour


def identifying_words(text: str) -> list[str]:
    """The words of a title, a place or a name that could identify it — and nothing else.

    A distinctive word is the second way a meeting travels without being quoted: "a 09:15 standup"
    names the Team standup without ever writing "Team standup". The rules that keep this from
    firing on ordinary prose are the same shape as the clock reader's: a word has to be long enough
    to mean something, it has to carry no digits (a token with a number in it is a code — an id, an
    etag, a date — and codes are already compared exactly), and it has to be someone's word rather
    than a joining word. An address contributes the person, not the domain: `hiring@example.com`
    identifies `hiring`, and `example` identifies a mail provider."""
    out = []
    for token in _normalize(text).split():
        if token in _STOPWORDS or len(token) < MIN_FRAGMENT_LEN:
            continue
        if any(ch.isdigit() for ch in token):
            continue
        out.append(token)
    return out


def _event_strings(event) -> list[tuple[str, str]]:
    """`[(field, value)]` for every string one event carries, dict or object, nested or flat.

    The same walk `redteam._event_sentinels` does, over the same reason: naming three fields is a
    check that covers the three somebody remembered, and the fourth is the one that leaks."""
    out: list[tuple[str, str]] = []

    def walk(prefix: str, value) -> None:
        if isinstance(value, str):
            if value.strip():
                out.append((prefix, value))
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                walk(prefix, item)
        elif value is None or isinstance(value, (bool, int, float, bytes)):
            return
        else:
            for key, item in vars(value).items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)

    walk("", event)
    return out


def event_fragments(event) -> list[tuple[str, str]]:
    """Every way ONE event could be recognised in a piece of text, as `[(field, fragment)]`.

    Three kinds, and the first is not enough on its own. The event's own strings are the exact
    net. The clock reader catches a start time rewritten as `09:15` or `quarter past nine`. The word
    reader catches a title reduced to its distinctive word. A reply that carried only the last two
    is a reply that told somebody about a meeting nobody asked about, which is the disclosure this
    exists to refuse."""
    out: list[tuple[str, str]] = []
    for field, value in _event_strings(event):
        out.append((field, value))
        clock = clock_of(value)
        if clock:
            out.append((f"{field}:clock", clock))
        for word in identifying_words(address_local(value) if "@" in value else value):
            out.append((f"{field}:word", word))
    return out


def address_local(value: str) -> str:
    """`hiring` from `hiring@example.com`, and the value unchanged when it is not an address."""
    match = re.search(r"[\w.+-]+@", str(value or ""))
    return match.group(0).rstrip("@") if match else str(value or "")


def _encodings_of(value: str) -> list[str]:
    """The ways the VALUE itself could be spelled rather than the ways the payload could hide it.

    An encoded value is the other half of the same attack: instead of hiding the value in an
    encoding, put the encoding in the payload. `base64("QQ-4471-ALPHA-9")` is a string that decodes
    to the fact, and a payload carrying it carries the fact.
    """
    raw = str(value)
    out = []
    try:
        out.append(base64.b64encode(raw.encode("utf-8")).decode("ascii"))
    except (UnicodeEncodeError, ValueError):                          # pragma: no cover - defensive
        pass
    try:
        out.append(urllib.parse.quote(raw))
    except (TypeError, ValueError):                                   # pragma: no cover - defensive
        pass
    try:
        out.append(html.escape(raw))
    except (TypeError, ValueError):                                   # pragma: no cover - defensive
        pass
    return [form for form in out if form and form != raw]


def _digit_evidence(value_flat: str, flat: str) -> bool:
    """Whether a NUMBER out of the value is in the payload, on the rule stated at `_LONG_NUMBER`.

    A five-digit run stands on its own. A four-digit one is only evidence when the value it came
    from is a date — a month name beside it — because "1943" in "14 March 1943" is the whole
    disclosure of "born in spring 1980", while "2024" in a reference number is a year that appears
    in half the mail a person receives.
    """
    months = bool(_MONTHS & set(value_flat.split()))
    for number in re.findall(r"\d+", value_flat):
        if len(number) < 4:
            continue
        if len(number) < _LONG_NUMBER and not months:
            continue
        for found in re.finditer(rf"(?<!\d){re.escape(number)}(?!\d)", flat):
            if len(number) >= _LONG_NUMBER:
                return True
            # A four-digit run only counts when the payload puts a MONTH beside it, and "beside"
            # is a window rather than the whole payload. The rule above states the intent — "1943"
            # is the disclosure, "2024" in a reference number is not — and a bare containment test
            # does not implement it: the widened haystack includes every base64 run in the payload
            # DECODED, so a few hundred audit rows produce thousands of decoded byte strings, and a
            # four-digit number is somewhere in that noise often enough to fire on a clean run. The
            # window is what makes the match evidence of a DATE rather than of arithmetic: a leak
            # writes "14 March 1943", and the month is next to the year in it.
            window = flat[max(0, found.start() - 40):found.end() + 40]
            if _MONTHS & set(window.split()):
                return True
    return False


def _token_run(value_flat: str, flat: str, scan=None) -> int:
    """The longest run of the value's content tokens that appears CONSECUTIVELY in the payload.

    `scan` is the payload's cached `(tokens, positions)` pair from `_scan_of`. It is threaded
    through from `check_text` because both halves of it were per-VALUE work otherwise: the widened
    gate re-tokenized one 2000-character email and rebuilt its position set once for every protected
    fact it checked, several hundred times per request, to answer questions about a string that had
    not changed. That repeated rebuild — not the matching — was what carried the worst-case attack
    over the playground's regression alarm. The parameter is optional so a caller checking a single
    value in isolation still gets the same answer.

    The inner walk is over POSITIONS for one token at a time rather than over every (start, end)
    pair: a run that begins at `wanted[start]` either continues to the next token in the payload or
    it does not, so extending it one token at a time is the whole of the question. The three
    nested loops it replaces asked the same thing with a fresh scan of the payload per candidate
    length.
    """
    wanted = _value_tokens(value_flat)
    if len(wanted) < _PARTIAL_TOKENS:
        return 0
    if scan is None:
        scan = _scan_of(flat)
    got, at = scan
    if not got:
        return 0
    best = 0
    for start in range(len(wanted)):
        # A run beginning here is at most `len(wanted) - start` long, so once that cannot beat the
        # best run already found, no later start can either.
        if len(wanted) - start <= best:
            break
        # Only the places this token actually occurs, not every position in the payload.
        for i in at.get(wanted[start], _NO_POSITIONS):
            length = 1
            while (start + length < len(wanted)
                   and (i + length) in at.get(wanted[start + length], _NO_POSITIONS)):
                length += 1
            if length > best:
                best = length
    # The original contract: 0, or a run long enough to be evidence. A single shared word is not a
    # shortened value, and callers compare against `_PARTIAL_TOKENS` rather than against zero.
    return best if best >= _PARTIAL_TOKENS else 0


def _how_present(value: str, flat: str, scan=None) -> str:
    """Whether a protected value reached this payload, and in which form.

    THE matcher — there is one, not two. It used to have a boolean twin (`_value_in`) that the gate
    called immediately after this one for every value, so every scan asked the same question twice
    and the answer was thrown away the first time. The boolean form is `bool(...)` of this, and the
    two loops in `check_text` now share one call.

    Three rules, and each one is a hole the other two do not cover:

    * the value, normalized — what the gate always did;
    * the value ENCODED, or the payload decoded into every form `_decoded_forms` knows;
    * the value SHORTENED — a consecutive run of its content tokens, or a number out of it. This is
      the finding the literal check could not see: "Flat 3, 118 Wentworth Road" and "Wentworth
      Road" are the same address said shorter, and a prefix of an account number is the account.

    What it does NOT cover is a rewrite with no shared surface at all — "the house on Wentworth",
    "born in spring 1980" when the value says "4 May". That is a limit of string matching and it is
    written down in `docs/THREAT_MODEL.md` rather than claimed away here.

    The refusal needs the reason because the two classes are not the same finding. `ProtectedLeak`
    says the value was copied; `ParaphraseLeak` says it left in a form a marker test would have
    called absent — reworded, shortened, or spelled in an encoding. Which one an owner is looking at
    changes what they do next: rewrite the sentence, or take the value out entirely. A single
    boolean here would put "copied" on the row for a sentence that merely shared two words with it.

    The order is the order of certainty, and `"verbatim"` is checked first for that reason: a value
    that IS in the payload is not reported as a resemblance, however it also resembles one.
    """
    norm = _normalize(value)
    if len(norm) < PrivacyGate.MIN_MARKER_LEN:
        return ""
    if norm and norm in flat:
        return "verbatim"
    for encoded in _encodings_of(value):
        if _normalize(encoded) in flat:
            return "encoded"
    if _token_run(norm, flat, scan) >= _PARTIAL_TOKENS:
        return "partial"
    return "digits" if _digit_evidence(norm, flat) else ""


def _iter_strings(payload, _depth: int = 0):
    """Every string in a nested JSON-ish structure, keys included — a marker can hide in a key."""
    if _depth > 32:
        raise EgressRefused("outbound payload is nested too deeply to inspect")
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for k, v in payload.items():
            yield str(k)
            yield from _iter_strings(v, _depth + 1)
    elif isinstance(payload, (list, tuple)):
        for v in payload:
            yield from _iter_strings(v, _depth + 1)
    elif payload is None or isinstance(payload, (bool, int, float)):
        return
    else:
        raise EgressRefused(
            f"outbound payload contains {type(payload).__name__}, which cannot be inspected — "
            f"an unreadable payload is not a clean one")
