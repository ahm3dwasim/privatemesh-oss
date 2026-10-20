"""The care circle, the protected facts, and the family digest.

One claim holds this module together: **an assistant running someone's week from their inbox
cannot be turned against the person they care for by anything it reads.** The engine already
refuses to move a meeting it was not asked about and refuses to tell anyone the thread did not
come from. Those are the scheduling half. This is the half that only exists when the person whose
week is being run is not the person holding the phone.

Three objects, and each is a rule rather than a feature:

**Protected facts.** An insurance or benefits id, a date of birth, a home address, a
power-of-attorney reference — the things that are not secrets in the cryptographic sense and are
exactly what a benefits-fraud email is fishing for. A protected fact is kept here, beside the
protected commitment, and behaves the same way: it is never in the object a model is handed (the
model gets a placeholder token), it is never in an outbound payload, and a refusal names the FIELD
without ever echoing the value. `mask_for_model` is the substitute step and `boundary.PrivacyGate`
is the check; the two are deliberately separate so that a masking bug is caught by the gate rather
than hidden by it.

**The care circle.** Who may ever receive anything, and what each of them may receive. This is a
standing limit in the same sense the six in `limits.py` are: a sentence the owner can read, a list
they wrote, and enforcement in trusted code. The load-bearing clause is the negative one — nothing
that is not the owner can add a member. Not an email, not a model answer, not a taught skill. An
address that arrives any other way is an address the engine falls back to asking about, which is
the answer that costs the attacker the thing they wanted.

**The family digest.** One outbound summary a day: what happened, what is coming, what needs the
owner. Composed the way the reply is — declared fragments with allowlisted sources, no free model
text — so the digest cannot become the free-text channel the reply is not allowed to be.

The direction this is built to is the one in `docs/THREAT_MODEL.md`: the model is assumed to be
fully on the attacker's side, and every green here has to be green with the model actively trying.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass, replace as _replace

#: The rule, in the words the owner reads. Written once because it is the `rule` on a decision row
#: AND the sentence a screen shows, and two spellings of one rule drift.
CARE_CIRCLE_RULE = "Send only to people on your care circle."

#: The disclosure half. The scheduling side says nothing about your protected commitment may pass
#: the gate; this says the same about a fact you marked private about the person you care for.
PROTECTED_FACT_RULE = ("A private fact about the person you care for never leaves without you.")

#: What a protected fact may be. A closed set rather than a free string: `kind` decides the
#: placeholder token, and a placeholder that could be spelled by the model would be a placeholder
#: the model could forge. An unknown kind is refused at the store rather than kept.
KINDS = ("benefits_id", "date_of_birth", "home_address", "document_ref", "phone", "account")

#: The one value of `added_by` a member may ever carry. Enumerated, so "only the owner can add a
#: member" is a fact about a closed set rather than a promise in a docstring.
OWNER = "owner"

_PLACEHOLDER_RE = re.compile(r"\[\[private:([a-z_]+)\]\]")


def placeholder(kind: str) -> str:
    """What the model sees in place of a protected fact's value.

    A token and not a redaction: `[REDACTED]` is `redact.py`'s word for a secret that was removed
    from text on its way out, and the two are different objects. This one is a NAME the engine and
    the model can both use — "the benefits id is [[private:benefits_id]]" is a sentence a model can
    reason with and cannot read anything out of. The `[[...]]` shape is chosen because it cannot
    occur in an address, a date or an id, so a placeholder cannot be confused with a value.
    """
    kind = str(kind)
    if kind not in KINDS:
        # The list, and NOT the value that was rejected. A refusal that quotes what it refused is
        # the disclosure the refusal exists to prevent — and here the refused string comes straight
        # off the request body, so an owner who typed a fact into the wrong field would find it
        # copied into the API's error, the server's log, and whatever reads either.
        raise UnknownKind("that is not a protected-fact kind this build has — expected one of "
                          f"{', '.join(KINDS)}")
    return f"[[private:{kind}]]"


class UnknownKind(ValueError):
    """A `kind` this build does not have. Refused rather than stored.

    Both closed kind lists raise this, and they are the same idea seen from two ends: a protected
    fact's `kind` decides its placeholder, and a member's `may_receive` decides which grant a send is
    checked against. A kind with no definition is a value with no mask, or a permission nobody can
    see the edges of, and neither is something to store.

    It is a `ValueError` and not a `CareRefused` so the API can tell it apart: "pick from this list"
    is a different answer to the client than "that address is malformed", and `care_kind_unknown`
    is a code the browser can render as the list itself.
    """


class NotTheOwner(RuntimeError):
    """Something other than the owner tried to change the care circle.

    Raised by the STORE rather than only by the route, because the route is not the only caller:
    a taught skill, a model answer or a future code path that reaches this object directly must hit
    the same wall. A guard that lives only at the HTTP boundary is a guard that the next caller
    does not have.
    """


class CareRefused(RuntimeError):
    """An outbound care action named someone, or something, the owner's own list does not cover."""


@dataclass(frozen=True)
class CareMember:
    """One person on the care circle, and what they may ever receive.

    `may_receive` is a list of action KINDS (`"digest"`, `"reply"`, `"schedule"`), not of topics:
    a topic list would be a second policy language, and the whole point of the six clauses in
    `limits.py` is that there is one enumerated list the owner can read and the code can enforce.
    """

    id: str
    name: str
    email: str
    may_receive: tuple[str, ...]
    added_by: str = OWNER
    added_at: str = ""

    @property
    def address(self) -> str:
        """The bare address, lowercased — what the provider routes on. A display name is chosen by
        whoever wrote the message, so comparison is never on it."""
        match = re.search(r"<([^>]+)>", self.email or "")
        return (match.group(1) if match else str(self.email or "")).strip().casefold()

    def as_dict(self) -> dict:
        """`docs/API_V2.md`: `{id, name, email, may_receive, added_by, added_at}`. The address is
        served as written; nothing here is a secret, and hiding it from the owner's own screen
        would make the list unreadable."""
        return {"id": self.id, "name": self.name, "email": self.email,
                "may_receive": list(self.may_receive), "added_by": self.added_by,
                "added_at": self.added_at}


@dataclass(frozen=True)
class CircleSnapshot:
    """The care circle as it stood at one instant, and what each person was listed for.

    Membership and grants are ONE object here rather than two questions asked of a store, and that
    is the whole point: `knows(addr)` followed by `may_receive(addr, kind)` is two reads of a list
    the owner can edit in between, so a member revoked between them is a member a send is cleared
    to reach, and a member added between them is one it is refused for. A snapshot is taken under
    the store's own lock and never changes afterwards, which is what makes "the circle as it stood
    when this was composed" a statement the send path can re-check rather than a memory.
    """

    #: When it was read, ISO-8601, so a refusal can say how old the reading was.
    taken_at: str
    members: tuple[CareMember, ...]

    def knows(self, address: str) -> bool:
        bare = _bare(address)
        return any(m.address == bare for m in self.members)

    def may_receive(self, address: str, kind: str) -> bool:
        bare = _bare(address)
        for member in self.members:
            if member.address == bare:
                return str(kind) in member.may_receive
        return False

    def addresses(self) -> set[str]:
        return {m.address for m in self.members}


@dataclass(frozen=True)
class ProtectedFact:
    """Something the owner marked private about the person they care for.

    There is no `value` in `as_dict` and there must never be one: the API serves `{id, label, kind,
    in_use}`, so a screen can show the owner their own list without the list itself becoming a
    readable copy of everything the gate exists to protect. The value is reachable only from the
    two places that need it — the masker and the gate.
    """

    id: str
    label: str
    kind: str
    value: str
    created_at: str = ""
    last_used_at: str = ""

    @property
    def placeholder(self) -> str:
        return placeholder(self.kind)

    @property
    def in_use(self) -> bool:
        """Whether this fact has been masked into a model payload since the sample day began.

        Derived from a recorded timestamp rather than asserted, because "in use" is exactly the
        kind of field a UI renders as a badge and exactly the kind that rots into a lie when it is
        a literal. A fact that has never been needed reads `false`, which is the honest answer.
        """
        return bool(self.last_used_at)

    def as_dict(self) -> dict:
        """NEVER the value. See the class docstring — this is the whole reason the class exists
        rather than serving the row."""
        return {"id": self.id, "label": self.label, "kind": self.kind, "in_use": self.in_use}


# ---------------------------------------------------------------- masking -----------------------
def mask_text(text: str, facts) -> tuple[str, list[str]]:
    """Replace every protected value in `text` with its placeholder.

    Returns the masked text and the ids that were masked, so a caller can record WHICH facts a
    model was shielded from without recording any part of them.

    Longest value first. A fact whose value contains another fact's value (an address that contains
    a postcode, a document reference that contains an id) would otherwise be masked in the wrong
    order and leave the tail of the longer one in the clear — which is the failure mode that makes
    a masker look like it worked.
    """
    masked: list[str] = []
    out = str(text)
    for fact in sorted(facts, key=lambda f: len(str(f.value)), reverse=True):
        value = str(fact.value or "")
        if len(value) < 4:
            # Shorter than the gate's own marker floor. A two-character "value" would match inside
            # ordinary words and turn the masker into a corruptor; the gate refuses it on the way
            # out regardless, which is the direction that fail-closes.
            continue
        pattern = re.compile(re.escape(value), re.IGNORECASE)
        if pattern.search(out):
            out = pattern.sub(fact.placeholder, out)
            masked.append(fact.id)
    return out, masked


def mask_payload(payload, facts) -> tuple[object, list[str]]:
    """`mask_text` over every string in a nested structure, keys included.

    The same walk `boundary._iter_strings` does, and for the same reason: a value can hide in a
    dict key, and a masker that only visited values would leave it there.
    """
    masked: list[str] = []

    def walk(node):
        if isinstance(node, str):
            new, hit = mask_text(node, facts)
            masked.extend(hit)
            return new
        if isinstance(node, dict):
            return {walk(k): walk(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(v) for v in node]
        return node

    return walk(payload), sorted(set(masked))


def find_placeholders(text: str) -> list[str]:
    """The fact kinds a text refers to by placeholder. Used to tell "the model was shown a
    placeholder" from "the model was shown nothing", which is the difference between a masked
    prompt and a prompt that never mentioned the thing."""
    return sorted(set(_PLACEHOLDER_RE.findall(str(text))))


# ---------------------------------------------------------------- the stores ---------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS care_circle(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL,
    may_receive TEXT NOT NULL DEFAULT '[]', added_by TEXT NOT NULL,
    added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS protected_facts(
    id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
    created_at TEXT NOT NULL, last_used_at TEXT NOT NULL DEFAULT '');
"""


class CareCircleStore:
    """Who may receive anything. Owner-writable, and nothing else.

    `add` takes `by` and refuses anything that is not `OWNER`. That parameter exists so the refusal
    is a fact about the call rather than about the caller's good intentions: the day a taught skill
    or a model answer grows a path to this object, it arrives with `by="model"` and is refused by
    name, which is a decision row an owner can read.
    """

    def __init__(self, conn: sqlite3.Connection | None = None, *, path: str | None = None,
                 now=None) -> None:
        self._lock = threading.Lock()
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._clock = now or (lambda: _dt.datetime.now().astimezone())

    def members(self) -> list[CareMember]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM care_circle ORDER BY added_at, id").fetchall()
        return [_member_of(r) for r in rows]

    def get(self, member_id: str) -> CareMember | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM care_circle WHERE id=?",
                                     (member_id,)).fetchone()
        return _member_of(row) if row is not None else None

    def snapshot(self) -> CircleSnapshot:
        """Membership and grants READ ONCE, as a frozen object both questions are answered from.

        One SELECT under one lock, rather than the two separate reads `knows` and `may_receive`
        each do. The difference is not efficiency: a caller that asks the two questions separately
        can be answered about two different circles, and the window between them is one the owner
        controls — they are editing this list from the other screen.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM care_circle ORDER BY added_at, id").fetchall()
            stamp = self._clock().isoformat(timespec="seconds")
        return CircleSnapshot(taken_at=stamp, members=tuple(_member_of(r) for r in rows))

    def addresses(self) -> set[str]:
        return self.snapshot().addresses()

    def knows(self, address: str) -> bool:
        """Whether an address is on the circle. Compared on the bare address, so a display name an
        attacker chose cannot make an outside address look like a member's."""
        return self.snapshot().knows(address)

    def add(self, *, name: str, email: str, may_receive=(), by: str = "",
            added_at: str = "") -> CareMember:
        """Add a member. Refuses anything but the owner — see the class docstring."""
        if str(by) != OWNER:
            raise NotTheOwner(
                f"the care circle may only be changed by the owner; this call arrived as "
                f"{str(by) or 'nobody'!r}. An address that reached this list any other way — from "
                f"an email, a model answer or a taught skill — is an address the engine asks you "
                f"about instead.")
        address = _bare(email)
        if not _looks_like_address(address):
            # NEITHER the address nor anything else the caller typed. The submitted string is the
            # one field most likely to BE the protected value — a benefits id pasted into the wrong
            # box — and this message reaches `api_v2._care_refusal`, which puts it in the response
            # body. The refusal says what is wrong with the FIELD and never what was in it.
            raise CareRefused("the email field is not an email address, so it cannot be a member")
        allowed = tuple(str(k) for k in may_receive)
        unknown = [k for k in allowed if k not in MAY_RECEIVE]
        if unknown:
            raise UnknownKind(
                f"may_receive names {len(unknown)} value(s) this build does not have — expected "
                f"any of {', '.join(MAY_RECEIVE)}")
        member = CareMember(id=f"cm-{uuid.uuid4().hex[:12]}", name=str(name).strip(),
                            email=str(email).strip(), may_receive=allowed, added_by=OWNER,
                            added_at=added_at or self._clock().isoformat(timespec="seconds"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO care_circle(id, name, email, may_receive, added_by, added_at) "
                "VALUES(?,?,?,?,?,?)",
                (member.id, member.name, member.email, json.dumps(list(member.may_receive)),
                 member.added_by, member.added_at))
            self._conn.commit()
        return member

    def remove(self, member_id: str, *, by: str = "") -> bool:
        if str(by) != OWNER:
            raise NotTheOwner("the care circle may only be changed by the owner")
        with self._lock:
            cur = self._conn.execute("DELETE FROM care_circle WHERE id=?", (member_id,))
            self._conn.commit()
        return bool(cur.rowcount)

    def may_receive(self, address: str, kind: str) -> bool:
        """Whether this address may ever receive this kind of thing. Both halves are required: an
        address not on the circle fails even for a kind it would be allowed, and a member fails for
        a kind the owner did not give them."""
        return self.snapshot().may_receive(address, kind)


#: The action kinds a member may be granted. Closed, like `limits.CLAUSES` and for the same
#: reason: a permission the owner cannot see the edges of is a permission they cannot judge.
MAY_RECEIVE = ("digest", "reply", "schedule")


class ProtectedFactStore:
    """The facts the owner marked private, in SQLite beside the boundary.

    Not versioned, unlike the protected commitment, and the difference is deliberate: a commitment
    is versioned because a change set BINDS to the version it was built against and a newer version
    has to be able to invalidate it. A protected fact is never an input to a plan — it is a thing
    that must not travel — so there is nothing for a version to protect. What it has instead is a
    `last_used_at`, which is what makes `in_use` a recorded fact rather than a guess.
    """

    def __init__(self, conn: sqlite3.Connection | None = None, *, path: str | None = None,
                 now=None) -> None:
        self._lock = threading.Lock()
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._clock = now or (lambda: _dt.datetime.now().astimezone())

    def facts(self) -> list[ProtectedFact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM protected_facts ORDER BY created_at, id").fetchall()
        return [_fact_of(r) for r in rows]

    def get(self, fact_id: str) -> ProtectedFact | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM protected_facts WHERE id=?",
                                     (fact_id,)).fetchone()
        return _fact_of(row) if row is not None else None

    def add(self, *, label: str, kind: str, value: str, by: str = "",
            created_at: str = "") -> ProtectedFact:
        """Add a fact. Owner-only, for the same reason `CareCircleStore.add` is: a model that
        could ADD a fact could add one, then have it masked into a prompt, then read the
        placeholder — which is a slower route to the same disclosure."""
        if str(by) != OWNER:
            raise NotTheOwner("protected facts may only be changed by the owner")
        placeholder(kind)          # refuses an unknown kind before anything is stored
        if len(str(value or "").strip()) < 4:
            raise CareRefused(
                "a protected fact shorter than 4 characters cannot be masked without matching "
                "ordinary words; the value was not stored")
        fact = ProtectedFact(id=f"pf-{uuid.uuid4().hex[:12]}", label=str(label).strip(),
                             kind=str(kind), value=str(value).strip(),
                             created_at=created_at or self._clock().isoformat(timespec="seconds"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO protected_facts(id, label, kind, value, created_at, last_used_at) "
                "VALUES(?,?,?,?,?,'')",
                (fact.id, fact.label, fact.kind, fact.value, fact.created_at))
            self._conn.commit()
        return fact

    def remove(self, fact_id: str, *, by: str = "") -> bool:
        if str(by) != OWNER:
            raise NotTheOwner("protected facts may only be changed by the owner")
        with self._lock:
            cur = self._conn.execute("DELETE FROM protected_facts WHERE id=?", (fact_id,))
            self._conn.commit()
        return bool(cur.rowcount)

    def mark_used(self, fact_ids, *, at: str = "") -> None:
        """Record that these facts were masked into something the model was handed.

        Called by the masker. This is what `in_use` reads, and it is stamped at the moment of use
        rather than at the moment of creation because "you marked this private" and "this was
        needed today" are different facts and a screen that conflated them would show every fact
        as in use forever.
        """
        ids = [str(f) for f in fact_ids if f]
        if not ids:
            return
        stamp = at or self._clock().isoformat(timespec="seconds")
        with self._lock:
            self._conn.executemany(
                "UPDATE protected_facts SET last_used_at=? WHERE id=?", [(stamp, i) for i in ids])
            self._conn.commit()

    # ---- what the gate needs ------------------------------------------------------------------
    def protected_values(self) -> list[tuple[str, str]]:
        """`[(label, value)]` — the shape `PrivacyGate.protected_values` already returns for a
        protected commitment. The label is what a refusal names, so a refusal says "the benefits
        id would have been disclosed" and never the number."""
        return [(f.label or f.kind, f.value) for f in self.facts()]


def _bare(value: str) -> str:
    match = re.search(r"<([^>]+)>", value or "")
    return (match.group(1) if match else str(value or "")).strip().casefold()


#: Deliberately permissive: this decides whether a string is address-SHAPED, not whether it is
#: deliverable. Validation that pretends to be delivery is a second thing to be wrong about.
_ADDRESS_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_address(value: str) -> bool:
    return bool(_ADDRESS_RE.match(value or ""))


def _member_of(row: sqlite3.Row) -> CareMember:
    try:
        may = tuple(json.loads(row["may_receive"]))
    except (TypeError, ValueError):
        may = ()
    return CareMember(id=row["id"], name=row["name"], email=row["email"], may_receive=may,
                      added_by=row["added_by"], added_at=row["added_at"])


def _fact_of(row: sqlite3.Row) -> ProtectedFact:
    return ProtectedFact(id=row["id"], label=row["label"], kind=row["kind"], value=row["value"],
                         created_at=row["created_at"], last_used_at=row["last_used_at"])


# ---------------------------------------------------------------- the digest ---------------------
#: What a digest may be BUILT from. These join `boundary.ALLOWED_FRAGMENT_SOURCES`, and they are
#: the same kind of thing: engine-composed text read off a record the engine holds, never prose a
#: model wrote. `care_happened` is read from the outcome of a change that ran; `care_coming` from
#: the calendar; `care_needs_you` from the open changes. A model cannot produce any of the three
#: because none of them is a string it supplies — each is rendered by this module from the record.
DIGEST_SOURCES = ("care_greeting", "care_happened", "care_coming", "care_needs_you")

#: The rule on a digest refusal, in the owner's words.
DIGEST_AUDIENCE_RULE = ("A digest goes only to your care circle, and only to the members you "
                        "listed for it.")

#: Why only one a day. Stated as a constant so the limit clause, the decision row and the refusal
#: sentence are one string rather than three that agree today.
DIGEST_PER_DAY = 1


@dataclass(frozen=True)
class DigestPlan:
    """What a digest would say, before anyone is shown it.

    A plan and not a message: the fragments are computed here, the gate checks them, and only then
    does anything become an `OutboundEmail`. The split is what lets the refusal name a FRAGMENT
    SOURCE, which is an actionable sentence, rather than a body, which is the thing that must never
    appear in a refusal.
    """

    day: str
    recipients: tuple[str, ...]
    fragments: tuple  # of boundary.Fragment
    #: Proof that this plan came out of a composer, and out of WHICH composer. Empty means nobody
    #: sealed it, which is the state every hand-built plan is in — and both doors refuse it. See
    #: `plan_seal`: a caller cannot compute one without the key, so this is not a flag.
    seal: str = ""
    #: The PORT's seal, laid over the composer's by whoever built the plan from its own reads.
    #: `compose_digest` is a public function, so its seal answers "a composer built this" and not
    #: "the ENGINE built this": a caller can compose a plan from a record set of its own making and
    #: satisfy every check `compose_digest` makes. `Executor.build_digest` composes from the
    #: calendar, the change store and the circle, and stamps this second seal with a key only that
    #: executor holds — so `send_digest` can prove the plan came from its own reads rather than from
    #: a caller that learned to call the composer.
    port_seal: str = ""

    def as_dict(self) -> dict:
        return {"day": self.day, "recipients": list(self.recipients),
                "fragments": [{"source": f.source, "text": f.text} for f in self.fragments]}


#: The composer's own key, minted per process and never exported. A `DigestPlan` a caller built by
#: hand carries no seal, and one a caller sealed BY HAND cannot be sealed correctly: the value is an
#: HMAC over the plan's artifact id under this key, and the key exists only inside this module.
#: Module-private rather than passed in because the whole point is that the port can check it
#: without trusting anything the caller supplied — a key the caller could name is a flag.
_PLAN_KEY = secrets.token_bytes(32)


def plan_seal(plan: DigestPlan, key: bytes) -> str:
    """The binding over a plan's OWN artifact: HMAC(key, artifact_id(plan)).

    The artifact id is the name of the exact bytes a disclosure approval is keyed on, so sealing it
    binds the plan to the artifact rather than to a flag beside it: change a word, a recipient, a
    record id or the day and the artifact moves, so the seal stops verifying. What the seal adds
    over the artifact id alone is that computing the id is not enough — the id is public, the key is
    not, so "this plan is the one the composer built" becomes something the port can CHECK instead
    of something the caller asserts.
    """
    return hmac.new(key, artifact_id(plan).encode("utf-8"), hashlib.sha256).hexdigest()


def seal_plan(plan: DigestPlan, *, key: bytes | None = None) -> DigestPlan:
    """The same plan, sealed. The one way a plan becomes one either door will accept."""
    return _replace(plan, seal=plan_seal(plan, key if key is not None else _PLAN_KEY))


def plan_is_sealed(plan: DigestPlan, *, key: bytes | None = None) -> bool:
    """Whether this plan carries the composer's proof. Compared in constant time.

    An unsealed plan answers False rather than raising, so both doors can decide what to say about
    it — and neither of them can decide to let it through by accident: `False` here is the only
    answer a hand-built plan can get, because the caller has no key to seal one with.
    """
    carried = str(getattr(plan, "seal", "") or "")
    if not carried:
        return False
    return hmac.compare_digest(carried, plan_seal(plan, key if key is not None else _PLAN_KEY))


def port_seal(plan: DigestPlan, key: bytes) -> str:
    """HMAC(key, the plan's composer seal) — the executor's mark over the composer's mark.

    Over the COMPOSER'S SEAL and not over the artifact again: the composer's seal already covers the
    artifact, so sealing it a second time binds the content transitively while making the two marks
    a chain rather than two independent claims. A plan whose words were edited after `build_digest`
    breaks the composer's seal, so the port's seal stops verifying too — one edit, both marks gone.
    """
    return hmac.new(key, str(getattr(plan, "seal", "") or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()


def seal_for_port(plan: DigestPlan, *, key: bytes) -> DigestPlan:
    """The plan as `Executor.build_digest` hands it over: the port's mark over the composer's."""
    return _replace(plan, port_seal=port_seal(plan, key))


def plan_is_port_sealed(plan: DigestPlan, *, key: bytes) -> bool:
    """Whether this plan is the one THIS executor built, compared in constant time.

    Both marks are required. The port's mark alone would be satisfied by any plan that executor
    sealed — including one it sealed without composing — and the composer's mark alone is what a
    caller can mint for itself. Together they say: a composer built these words, AND this executor
    built this plan, from records it read itself.
    """
    if not plan_is_sealed(plan):
        return False
    return hmac.compare_digest(str(getattr(plan, "port_seal", "") or ""), port_seal(plan, key))


class DigestPlanForged(CareRefused):
    """A plan that did not come out of the engine's own composer.

    Raised rather than ignored, at the door, because the alternative — reading the plan and sending
    it anyway — is the demonstrated finding: a caller-built `DigestPlan` reached `digest_email` and
    put `OWNER NEVER APPROVED THIS` in the body of a message to the owner's care circle. Nothing
    about the plan's own content can be checked for that: the pieces were well-formed, the sources
    allowlisted, the render exact. What was missing was that no composer ever built it.
    """


_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS care_digests(
    id TEXT PRIMARY KEY, day TEXT NOT NULL, recipients TEXT NOT NULL,
    artifact TEXT NOT NULL, sent_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS care_digests_one_per_day ON care_digests(day);
"""


class DigestAlreadySent(RuntimeError):
    """A second digest for a day that already has one. Raised by the STORE, not by a caller's check.

    The cap is one a day, and the only way to enforce "one" is for the second writer to be refused
    by the same object that accepted the first. A count-then-insert cannot do it: both workers read
    zero and both insert, and the report says one digest went out when two did.
    """


class DigestDayMissing(RuntimeError):
    """A digest with no day to be one-per. An empty day is not a day nobody has sent one for — it
    is a bucket that every day falls into, so the first empty-day digest would refuse every real
    digest for the rest of time, and an attacker who controls the field could pick the bucket."""


class DigestLedger:
    """Which digests have gone out, by day. The thing `DIGEST_PER_DAY` is counted against.

    A table and not a counter, because the question clause 7 asks is "for THAT day" and a counter
    cannot answer it — a process that ran across midnight would refuse the new day's digest or
    allow a second one on the old, depending on which way it was written. The rows also carry the
    recipients, which is what lets a refusal say how many people already received one.
    """

    def __init__(self, conn: sqlite3.Connection | None = None, *, path: str | None = None,
                 now=None) -> None:
        self._lock = threading.Lock()
        self._conn = conn if conn is not None else sqlite3.connect(
            path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_LEDGER_SCHEMA)
        self._conn.commit()
        self._clock = now or (lambda: _dt.datetime.now().astimezone())

    def sent_on(self, day: str) -> int:
        """How many digests have gone out for this day. Zero for an empty or unknown day rather
        than an exception: a bundle that never said which day it was for is a bundle clause 7
        cannot clear, and the caller checks that separately — this is only the count.

        Counted IGNORING `sent_at`, so a digest that is in flight holds the day. That is what makes
        the cap a cap rather than a race: see `claim`.
        """
        if not day:
            return 0
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM care_digests WHERE day=?",
                                     (str(day),)).fetchone()
        return int(row["n"]) if row is not None else 0

    def claim(self, *, day: str, recipients, artifact: str) -> str:
        """Take the day ATOMICALLY, before anything is sent. Returns the entry id.

        One INSERT, and the UNIQUE index on `day` is what decides — not a count read a moment
        earlier. Two processes that both believe the day is free produce one row and one
        `DigestAlreadySent`, which is the only shape in which "one a day" is true rather than
        usually true.

        `sent_at` is written EMPTY: the row exists from the moment the day is taken, and `confirm`
        is what makes it a digest that went out. A claim whose send fails is `release`d, so the
        ledger's steady state is exactly "the digests that were actually sent" while the cap holds
        against everything that is in flight.
        """
        if not str(day or "").strip():
            raise DigestDayMissing("a digest must say which day it is for; an empty day is a bucket "
                                   "every day falls into rather than a day nobody has sent one for")
        entry_id = f"dg-{uuid.uuid4().hex[:12]}"
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO care_digests(id, day, recipients, artifact, sent_at) "
                    "VALUES(?,?,?,?,'')",
                    (entry_id, str(day), json.dumps([str(r) for r in recipients]),
                     str(artifact)))
            except sqlite3.IntegrityError as exc:
                raise DigestAlreadySent(
                    f"a digest has already gone out for {day}; the limit is "
                    f"{DIGEST_PER_DAY} a day") from exc
            self._conn.commit()
        return entry_id

    def confirm(self, entry_id: str, *, sent_at: str = "") -> None:
        """Stamp the moment this digest actually left. The step that turns a claim into a record."""
        with self._lock:
            self._conn.execute(
                "UPDATE care_digests SET sent_at=? WHERE id=?",
                (sent_at or self._clock().isoformat(timespec="seconds"), str(entry_id)))
            self._conn.commit()

    def release(self, entry_id: str) -> None:
        """Give the day back. Called when the send FAILED, so a failure costs nothing but the
        attempt and does not burn the owner's one digest for the day."""
        with self._lock:
            self._conn.execute("DELETE FROM care_digests WHERE id=?", (str(entry_id),))
            self._conn.commit()

    def record(self, *, day: str, recipients, artifact: str, sent_at: str = "") -> str:
        """A digest that has already gone out. `claim` + `confirm` in one call, for a caller that
        is recording history rather than about to send — and `claim`'s uniqueness applies to it
        too, which is the point: there is no door into this table that ignores the cap."""
        entry_id = self.claim(day=day, recipients=recipients, artifact=artifact)
        self.confirm(entry_id, sent_at=sent_at)
        return entry_id

    def recent(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM care_digests ORDER BY sent_at DESC, id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [{"id": r["id"], "day": r["day"], "recipients": json.loads(r["recipients"]),
                 "artifact": r["artifact"], "sent_at": r["sent_at"],
                 # A row with no `sent_at` is a day TAKEN and not yet spent. Reported as its own
                 # field rather than left for a reader to infer from an empty timestamp, because an
                 # empty string reads as "the epoch" to anything that parses it as a date.
                 "pending": not r["sent_at"]} for r in rows]


#: The digest's fixed opening. A CONSTANT rather than a parameter: a caller-supplied greeting is
#: free text under an allowlisted label, which is exactly the channel this module exists to close.
#: If it ever needs to vary it varies here, where the change is a diff against one line.
DIGEST_GREETING = "Here is the day at a glance."

#: The one fixed prefix a digest's subject carries. A member of `boundary.SUBJECT_TEMPLATES`, which
#: is the closed set of them — the two constants are pinned against each other by `test_care.py`,
#: so a third prefix cannot be added on one side only.
DIGEST_SUBJECT_TEMPLATE = "Today —"


@dataclass(frozen=True)
class DigestLine:
    """One line of a digest, and the RECORD it was rendered from.

    The `source` is not decoration and not a claim the composer takes on trust. `record_id` names
    the calendar event or the change the text came out of, and `compose_digest` refuses a line that
    has none — so "this sentence is `care_coming`" is a statement about a record that exists rather
    than a label somebody typed next to a string. A model handed `compose_digest` cannot produce a
    line at all: it has no record to name.

    The id is not only required here, it is CARRIED: `compose_digest` puts it on the fragment it
    renders, so the piece of the digest that is checked downstream names the same record this line
    does. Required-and-dropped was the state the fifth review found — the line was asked for an id
    and the fragment got an empty `event_id`, which left the body-equality check with nothing to
    compare against and blessed whatever text arrived under the label.
    """

    source: str
    record_id: str
    text: str

    def __post_init__(self) -> None:
        if self.source not in DIGEST_SOURCES or self.source == "care_greeting":
            raise DigestBadLine(
                f"a digest line's source must be one of "
                f"{', '.join(s for s in DIGEST_SOURCES if s != 'care_greeting')}")
        if not str(self.record_id or "").strip():
            raise DigestBadLine(
                "a digest line must name the record it was rendered from; a sentence with no "
                "record behind it is prose, whichever label it carries")


class DigestBadLine(ValueError):
    """A digest line that does not say where it came from. Refused rather than labelled."""


def compose_digest(*, day: str, recipients, happened=(), coming=(), needs_owner=(),
                   records) -> DigestPlan:
    """Build the day's digest out of lines that each name the record they came from.

    Every argument is a `DigestLine` (or a list of them). A bare string is REFUSED, and that refusal
    is the finding this signature exists for: with `str(line)` accepted, an allowlisted source was a
    label anybody could put on any text, so "care_happened" proved nothing about where the sentence
    came from — a compromised model could write the disclosure itself and sign it with a name from
    the allowlist. Rendering stays the caller's job, because the caller is the one holding the
    calendar and the change records; naming the record is now part of the job.

    `record_id` travels ONTO the fragment, which is the fifth review's fourth finding. It used to be
    demanded by `DigestLine` and then dropped here, so every digest fragment carried an EMPTY
    `event_id` — and the body-equality check downstream approves a body that is the render of its
    own declared pieces, so an unrendered line arrived at that check already blessed: the line said
    `care_coming`, the fragment said nothing, and nothing had to agree with anything. With the id
    carried through, a digest fragment names the same record its line named, and the two are one
    claim rather than two.

    `records` is the mapping from record id to THE ENGINE'S OWN RENDER OF THAT RECORD, and it is
    required. The sixth review's fourth finding is that the set of ids this used to take proved
    membership and not provenance: `DigestLine(record_id="known-event", text="<anything>")` passed,
    because a known id is a citation and not evidence — the sentence beside it was never compared
    with the record it named. Handing over the render instead of the id makes the comparison
    possible, and there is exactly one renderer for each source (`execute.build_digest` renders the
    lines FROM this mapping), so the text cannot drift from the record it claims.

    There is no `None` for `records` any more. `None` meant "the caller has not offered a record
    set", and a line's own id was then all there was — which is precisely the state the finding
    describes: a known-looking id with nothing behind it. A caller that cannot say what its records
    render cannot compose a digest.
    """
    from .boundary import Fragment

    known = {str(k): str(v) for k, v in dict(records).items()}
    fragments = [Fragment("care_greeting", DIGEST_GREETING)]
    for lines, source in ((happened, "care_happened"), (coming, "care_coming"),
                          (needs_owner, "care_needs_you")):
        for line in lines:
            if not isinstance(line, DigestLine):
                raise DigestBadLine(
                    f"a digest fragment is {type(line).__name__}, not a DigestLine — text with no "
                    f"record behind it does not become a digest line")
            if line.source != source:
                raise DigestBadLine(
                    f"a line rendered from a record was filed under '{line.source}' but passed as "
                    f"'{source}'")
            if str(line.record_id) not in known:
                raise DigestBadLine(
                    f"a digest line names record {line.record_id!r}, which is not a record this "
                    f"build can read — a sentence attributed to a record that does not exist is "
                    f"prose with a citation")
            if str(line.text) != known[str(line.record_id)]:
                # THE PROVENANCE CHECK, and it is the whole of the sixth review's fourth finding.
                # A known id made the line LOOK attributed; nothing made it true. Comparing the
                # sentence with the render of the record it names is the difference between a
                # citation and evidence, and it costs one lookup because the caller already had to
                # render the record to write the line at all.
                raise DigestBadLine(
                    f"a digest line names record {line.record_id!r} but its words are not that "
                    f"record's render — a sentence attributed to a record has to be the sentence "
                    f"that record produced")
            fragments.append(Fragment(source, line.text, event_id=str(line.record_id)))
    # SEALED ON THE WAY OUT, and this is the composer's signature rather than a convenience. A
    # `DigestPlan` is a frozen dataclass, so a caller can build one that satisfies every check this
    # function makes — allowlisted sources, records that exist, text that IS the record's render —
    # and hand it straight to a door. The eighth review's first finding is that this was enough:
    # `digest_email(DigestPlan(...))` produced an outbound email whose body read `OWNER NEVER
    # APPROVED THIS`, and `compose_digest`'s provenance only ever protected the normal path. The
    # seal is what makes "the engine composed this" checkable at the door instead of assumed.
    return seal_plan(DigestPlan(day=str(day), recipients=tuple(str(r) for r in recipients),
                                fragments=tuple(fragments)))


def artifact_id(plan: DigestPlan) -> str:
    """A name for THIS digest's exact bytes: day, recipients, and every fragment in order.

    Content-addressed rather than a random id, because the approval the direction asks for is for
    *that exact artifact*. A random id would stay the same while the digest changed under it, so an
    owner who approved Tuesday's digest at nine would have approved whatever the digest said at
    five — which is the hole the phrase "exact artifact" exists to close. Hash the words, and an
    edit invalidates the approval the way it should: by not matching.

    `event_id` is IN the payload, and the sixth review's fourth finding is why. The hash used to
    cover `[source, text]` alone, so two fragments with the same words and different records hashed
    the same — which means swapping a record id, the very field this module's provenance claim
    rests on, left the artifact identity untouched and the approval the owner gave still "matched".
    A digest is identified by what it says AND by what each sentence says it came from.
    """
    payload = {"day": plan.day, "recipients": list(plan.recipients),
               "fragments": [[f.source, f.text, str(getattr(f, "event_id", "") or "")]
                             for f in plan.fragments]}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    import hashlib
    return "digest-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def digest_subject_fragments(plan: DigestPlan):
    """The day, as the one piece of a digest's subject that varies.

    `Today — ` is the fixed prefix and the day is the piece behind it, exactly as `Re: ` is for a
    reply. It is a FRAGMENT with a source rather than a finished string because the sixth review's
    first finding is the same field one port over: a subject handed over whole reaches the
    provider's `Subject` header with nothing having rendered it, and a digest's subject is no more
    exempt from that than a reply's is."""
    from .boundary import Fragment

    return (Fragment(source="day", text=str(getattr(plan, "day", "") or "")),)


def digest_subject(plan: DigestPlan) -> str:
    """`Today — <the day>`: the render, for readers that want the finished string."""
    from .boundary import render_subject

    return render_subject(DIGEST_SUBJECT_TEMPLATE, digest_subject_fragments(plan))


def digest_email(plan: DigestPlan, *, gate, change_id: str = "",
                 care_circle=None, snapshot=None):
    """Turn a checked plan into the one email a digest may be.

    The audience is checked TWICE and both are load-bearing. `care_circle` is passed to `compose_email`
    so the gate refuses a recipient the owner did not list — that is clause 7 at the moment of
    composition, and it is the check that names the mistake while the stack still says which step
    built the message. The port checks again at send time, because a message can be composed once
    and sent later, and the second check is the one that cannot be skipped.

    `artifact` is the digest's content hash, and it is what an owner's disclosure approval is keyed
    on. Passing it does NOT by itself let a protected value through: the approval set is empty
    until the owner approves something, and `_disclosure_allowed` refuses on an empty audience or a
    mismatch, so the default behaviour of this function is to refuse a protected value.

    The circle is read ONCE, here, into a `CircleSnapshot`, and the snapshot travels ON the email.
    The audience check then runs twice against the SAME reading — at composition and again at the
    port — which is the only version of "twice" that means anything. Two live lookups would be two
    different answers about a list the owner is editing, and the second would be checking the
    message against a circle nobody composed it for.
    """
    from .boundary import CareAudienceRefused, compose_email

    # FIRST, before the audience and before anything is rendered: did a composer build this? The
    # order is the finding's. Every check below reads the plan, and every one of them can be
    # satisfied by a plan nobody composed — the pieces well-formed, the sources allowlisted, the
    # render exact — because none of them is a question about ORIGIN. Asked here, and asked by the
    # port too, because this function is reachable directly and the port is the door that matters.
    if not plan_is_sealed(plan):
        raise DigestPlanForged(
            "refused: this digest plan did not come out of the engine's composer, so there is "
            "nothing that says a record produced its sentences. A plan is composed from the "
            "calendar and the change records, and one assembled by hand is prose with a digest's "
            "shape.")
    if not plan.recipients:
        # A digest with no audience is not "a digest nobody objected to". Refused here rather than
        # sent to an empty list, because the empty list is what a failed lookup of the care circle
        # looks like, and silently succeeding on it would make the failure mode invisible.
        raise CareRefused(
            "refused: this digest has no recipients. A digest goes to your care circle, and an "
            "empty audience is not one — it is what a circle that could not be read looks like.")
    if snapshot is None:
        store = care_circle if care_circle is not None else getattr(gate, "care_circle", None)
        if store is None:
            raise CareAudienceRefused(
                "refused: this build has no care circle, so there is nobody a digest may be sent "
                "to. Add the people who may receive it first.")
        snapshot = store.snapshot()
    return compose_email(to=plan.recipients, subject_template=DIGEST_SUBJECT_TEMPLATE,
                         subject_fragments=digest_subject_fragments(plan),
                         fragments=plan.fragments, gate=gate, change_id=change_id,
                         artifact=artifact_id(plan), care_kind="digest",
                         care_snapshot=snapshot)
