"""The v2 HTTP surface — docs/API_V2.md, served over the engine and nothing else.

This module holds no policy. It reads what the engine recorded, maps the engine's refusals onto
status codes, and refuses to invent a shape the UI would then have to guess at:

* `StaleChangeSet` is a **409**, because the request was well-formed and the world moved — the
  owner is looking at a plan that is no longer the plan, and the UI must re-render rather than
  retry.
* `EgressRefused` is a **422**, and the detail says WHICH field and WHICH rule without quoting
  what leaked. An error message is an outbound payload too.
* `NoFit` is a **422** carrying the validator's own sentence, because "16:00 does not fit" is an
  answer with numbers in it, not a validation complaint.
* `ReauthRequired` is a **503**, because nothing about the request was wrong — the provider
  connection is not there, and the one action that fixes it ("Reconnect Google") is the owner's.
  Anything that reaches a provider can raise it, and a 500 would tell the UI to retry a call that
  cannot succeed until a human consents again. A connection that was never made is a different
  thing and is a **409 `connection_required`**: one is "the provider stopped accepting us", the
  other is "you have not connected this yet", and the owner's next move differs.

`V2Service` is deliberately a plain object rather than a bundle of route handlers: lane ui2 can
import it and call the same methods the HTTP layer calls, and the tests in `tests/test_api_v2.py`
go through HTTP so the contract itself is what is pinned.

Every route is wrapped in `@coded`, which renders a refusal as `{detail, code}` — the two fields
`docs/API_V2.md` names. The code is not decoration: `web/lib/api.ts` puts it on `ApiError.code`
and the UI branches on it, so a refusal that arrives as prose with a status attached is a refusal
the browser cannot act on.
"""
from __future__ import annotations

import datetime as _dt
import functools
import inspect
import json
import re
import threading
import typing
import uuid
import weakref

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.routing import Match, get_route_path

from . import demo_session
from . import limits as limit_rules
from . import playground
from .agreements import AgreementChecker, AgreementStore
from .boundary import CareAudienceRefused, EgressRefused, PrivacyGate
from .changes import DONE, PARTIAL, STOPPED, NoFit
from .changeset import PENDING_STATUS, APPROVED_STATUS, StaleChangeSet
from .execute import CALENDAR_UPDATE_KIND, EMAIL_REPLY_KIND, DigestLedgerMissing
from .learn import CAPABILITY, ApprovalNotReady, ApprovalStale
from .limits import LimitStore, UnknownMode
from .teach import RuleRefused
from .owner_token import bearer, matches
from .ports import EtagMismatch, ReauthRequired

#: What the session screen shows before any connector is attached. Four services, four rows —
#: and the detail says out loud that these are simulators, because a "connected" badge on
#: synthetic data is the kind of small lie that makes every other badge worthless.
SAMPLE_CONNECTIONS = (
    {"service": "gmail", "state": "connected", "detail": "sample mailbox (synthetic)"},
    {"service": "calendar", "state": "connected", "detail": "sample calendar (synthetic)"},
    {"service": "tavily", "state": "connected", "detail": "sample search results (synthetic)"},
    {"service": "nemotron", "state": "connected", "detail": "sample model replies (synthetic)"},
)

#: The services the connections screen can name. Anything else is a 404 rather than a row invented
#: on the spot: a connection to a service that does not exist is not a connection.
SERVICES = tuple(row["service"] for row in SAMPLE_CONNECTIONS)

#: `HH:MM`, 24-hour. The same shape the browser's `<input type="time">` produces and the same
#: shape `Session.boundary.start` reports, so the value the owner typed round-trips unchanged.
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

#: A buffer outside this is refused. 0–240 is the range `docs/API_V2.md` states and the range the
#: web client validates against, so the browser and the server refuse the same inputs.
MAX_BUFFER_MINUTES = 240

#: The day window the boundary start must fall inside. A "protected start" at 23:50 with a
#: 45-minute notice describes a commitment that begins before the day it protects.
_DAY_START, _DAY_END = "00:00", "23:59"


#: The refusal code each owner-only route reports. One table so the gate and the client cannot
#: drift: `web/lib/api.ts` puts the code on `ApiError.code` and the UI branches on these strings,
#: and the second review's contract already fixed them for the two routes that had a gate.
APPROVAL_FORBIDDEN = "approval_forbidden"
DECISIONS_FORBIDDEN = "decisions_forbidden"
OWNER_FORBIDDEN = "owner_forbidden"
UNKNOWN_LIMITS_MODE = "unknown_limits_mode"
#: The refusal `approve` reports while the owner's standing mode is `off`. Its own code because it
#: is its own situation: the request was the owner's, the plan is current, and the standing answer
#: says nothing runs. A client that showed the generic stale-plan message here would send the owner
#: to re-prepare a plan that is perfectly good.
LIMITS_OFF = "limits_off"


class V2Error(HTTPException):
    """A refusal with a code.

    Subclasses `HTTPException` so that even if one escapes the `@coded` wrapper — through a
    handler FastAPI runs itself — the client still gets a readable body rather than a 500."""

    def __init__(self, status_code: int, detail: str, code: str) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


#: The most a request body may be, re-exported from `playground` because that is where the caps
#: live and this is only the name the route imports. A cap defined next to the route is a cap the
#: next route does not get.
MAX_REQUEST_BYTES = playground.MAX_REQUEST_BYTES


def _care_refusal(exc) -> "V2Error":
    """A store refusal on owner-supplied input, as a 422 rather than a 500.

    `care.CareRefused` and `care.UnknownKind` are raised by the STORE, which is where they belong —
    the guard has to hold for every caller, not only the ones that arrive over HTTP. But the values
    that trip them (`may_receive`, `kind`, a malformed address) are supplied by the CLIENT, and an
    unhandled one is this API's bug rather than the caller's mistake. Mapping them here keeps the
    store's fail-closed behaviour and stops a typo in a request body from reading as a crash.

    THE BODY IS NEVER `str(exc)`, and that is a fix rather than a preference. An exception message
    is a place a value can leave this process without passing the gate: these refusals are raised
    while looking at owner-supplied input, and the store's own messages are built to name the KIND
    of mistake rather than quote the value. Echoing the message would make that discipline the only
    thing standing between a protected value and an HTTP response body — so what the client gets is
    the exception's TYPE, and the store keeps the sentence. The one sentence worth keeping is the
    unknown-kind one, and it names the allowlist rather than what the caller sent.

    `NotTheOwner` is deliberately NOT mapped. Every route that can reach it proves the owner first,
    so one arriving here is a wiring defect and an unhandled exception is the honest report.
    """
    from . import care as care_rules

    # The digest's three, before the generic care branch, and each answer is a FIXED sentence. The
    # store's own messages name the day — which is the one thing the caller supplied — and echoing
    # them would put caller-controlled text in an HTTP body by the same route finding 15 closed for
    # everything else. The code is what a client branches on; the sentence is what a person reads.
    if isinstance(exc, care_rules.DigestAlreadySent):
        return V2Error(409, "a digest has already gone out today, and the limit is one a day. "
                            "Nothing was sent.", code="digest_already_sent")
    if isinstance(exc, care_rules.DigestDayMissing):
        return V2Error(422, "a digest has to say which day it is for. Nothing was sent.",
                       code="digest_day_missing")
    if isinstance(exc, DigestLedgerMissing):
        return V2Error(409, "this build cannot tell whether a digest has already gone out, so it "
                            "will not send one. Nothing was sent.", code="digest_ledger_missing")
    if isinstance(exc, care_rules.UnknownKind):
        return V2Error(422, f"that is not a kind of fact this build keeps; the kinds are "
                            f"{', '.join(care_rules.KINDS)}", code="care_kind_unknown")
    if isinstance(exc, CareAudienceRefused):
        # The circle's refusal, and it is NOT `CareRefused`: that one is the store rejecting a body
        # the owner typed, while this one is the egress gate refusing a recipient. Both are 422 and
        # the caller's next move is the same — list the person, or send somewhere else — so the
        # sentence says which of the two happened rather than making the client parse a code for a
        # distinction only the screen cares about. Imported from `boundary` because that is where it
        # lives; naming it `care_rules.CareAudienceRefused` raised AttributeError inside an
        # `except` clause, which is a 500 wearing a refusal's clothes.
        return V2Error(422, "a care action named an address that is not on your care circle. "
                            "Nothing was sent.", code="care_audience_refused")
    if isinstance(exc, care_rules.CareRefused):
        return V2Error(422, "the care store refused that: it does not match what the owner has "
                            "listed. Nothing was changed.", code="care_refused")
    return V2Error(422, "the care store refused that. Nothing was changed.", code="care_refused")


def coded(handler):
    """Render this route's refusals as `{detail, code}`.

    `functools.wraps` is load-bearing, not tidiness: FastAPI reads the handler's signature to
    build the request model, and it follows `__wrapped__`, so the path parameters and the pydantic
    body keep working through the wrapper. The handler stays synchronous, which keeps it on the
    threadpool FastAPI would have used anyway — execution reaches real providers, and blocking the
    event loop for it would make one slow approval everybody's slow approval."""

    # A route that has to look at the bytes before FastAPI parses them cannot be synchronous: the
    # ASGI receive channel is async, and there is no synchronous door onto it. So the wrapper keeps
    # the handler's colour instead of forcing one on it — a sync handler stays on the threadpool
    # FastAPI would have used anyway, and an async one is awaited on the event loop.
    if inspect.iscoroutinefunction(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except V2Error as exc:
                return JSONResponse(status_code=exc.status_code,
                                    content={"detail": exc.detail, "code": exc.code})
        return wrapper

    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except V2Error as exc:
            return JSONResponse(status_code=exc.status_code,
                                content={"detail": exc.detail, "code": exc.code})

    return wrapper


async def _read_capped(request: Request, limit: int = MAX_REQUEST_BYTES) -> bytes:
    """This request's raw body, refused the moment it is past `limit`.

    Two guards, because either one alone is bypassable. `Content-Length` catches the honest
    oversized request without reading a byte of it, and is absent from a chunked one; the running
    count catches that, and catches a header that lied. Neither parses anything: the cap has to
    fire on BYTES, because the thing being protected is the memory that parsing them would take.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise V2Error(413, f"the request body is {declared} bytes; the playground accepts "
                           f"{limit}", code="playground_body_too_large")
    chunks: list[bytes] = []
    seen = 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > limit:
            raise V2Error(413, f"the request body is past the {limit} bytes the playground "
                               f"accepts", code="playground_body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _gate_prompts(view: dict, boundary, gate=None) -> dict:
    """Every `prompt_sent` in a model-view, put through the gate the live build would apply.

    The gate is asked one prompt at a time so that a refusal costs one field rather than the whole
    response — the same rule the playground's own response filter follows. The refusal is recorded
    as a `withheld` row on the call it belongs to, which is the shape `changes._ask` already writes
    when the gate refuses a payload BEFORE it is sent: the two refusals are the same event at
    different moments, and a reader should not have to learn two shapes to see either.

    `gate` is the ENGINE's gate when the caller has one, and that is the fix rather than a
    convenience. A gate built here from the boundary alone knows about the protected COMMITMENT and
    nothing about the care facts, so on the build where a benefits id is the thing most likely to
    be in a prompt it was the one value this check could not see — the second refusal channel was
    blind to exactly the values the first one exists for. The engine's gate carries the facts and
    the circle; the fallback is for a caller that has neither, and it is the narrower check, not a
    different policy.
    """
    gate = gate if gate is not None else PrivacyGate(boundary)
    check = getattr(gate, "check_text", None)
    if check is None:                                                 # pragma: no cover - defensive
        return view
    for call in view.get("calls") or []:
        prompt = call.get("prompt_sent")
        if prompt is None:
            continue
        try:
            check(json.dumps(prompt, ensure_ascii=False, default=str), kind="model_view")
        except EgressRefused as exc:
            call["prompt_sent"] = None
            call["withheld"] = list(call.get("withheld") or []) + [
                {"field": exc.field, "reason": str(exc)}]
    return view


def _demo_email_body(raw: bytes) -> dict:
    """The demo email out of an already-size-checked body, or a sentence saying what was wrong.

    The same shape as `_attack_email`, and for the same reason: the visitor is the one who reads
    these, and "body -> message_id -> string should be a string" is not an answer to anything. A
    `message_id` is left to `V2Service.demo_email` to resolve, so an unknown one is a 404 naming
    the id rather than a pydantic 422 with a JSON pointer in it."""
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise V2Error(422, "the request body was not JSON; the demo takes "
                           '{"message_id": "...", "body_text": "..."}', code="demo_limit")
    if not isinstance(payload, dict):
        raise V2Error(422, 'the request body must be an object with "message_id" and "body_text"',
                      code="demo_limit")
    for field in ("message_id", "body_text"):
        if not isinstance(payload.get(field, ""), str):
            raise V2Error(422, f'"{field}" must be a string', code="demo_limit")
    return {"message_id": payload.get("message_id", ""), "body_text": payload.get("body_text", "")}


def _attack_email(raw: bytes) -> str:
    """The email out of an already-size-checked body, or a sentence saying what was wrong.

    Every refusal here is a `playground_limit` with a readable message rather than a pydantic 422
    with a JSON pointer in it — the same contract `playground.EmailTooLong` has, and for the same
    reason: this route's errors are read by whoever is trying to attack the demo, and "body -> email
    -> string should have at most 2000 characters" is not an answer to anything.
    """
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise V2Error(422, "the request body was not JSON; the playground takes "
                           '{"email": "..."}', code="playground_limit")
    if not isinstance(payload, dict):
        raise V2Error(422, 'the request body must be an object with an "email" field',
                      code="playground_limit")
    email = payload.get("email", "")
    if not isinstance(email, str):
        raise V2Error(422, '"email" must be a string', code="playground_limit")
    if len(email) > playground.MAX_EMAIL_CHARS:
        # The same sentence `playground.attack` raises, kept identical on purpose: which of the two
        # guards fired is an implementation detail, and a caller that learned to tell them apart
        # would be reading this file's structure rather than its contract.
        raise V2Error(422, f"the email is {len(email)} characters; the limit is "
                           f"{playground.MAX_EMAIL_CHARS}", code="playground_limit")
    return AttackBody(email=email).email


class RoutineBody(BaseModel):
    paused: bool


class BoundaryBody(BaseModel):
    """Deliberately loose: a bad time or a bad buffer is refused by `set_boundary` with the
    `invalid_boundary` code the web client branches on. Letting pydantic reject it first would
    answer a 422 whose body has no code, which is the shape this contract exists to remove."""

    start: str = ""
    buffer_minutes: int = 0


class EditBody(BaseModel):
    reply_body: str | None = None
    start: str | None = None
    venue_index: int | None = None


class ApproveBody(BaseModel):
    version: int


class LimitsBody(BaseModel):
    """Deliberately loose, like `BoundaryBody`: an unknown mode is refused by `set_limits` with the
    `unknown_limits_mode` code the caller branches on. Letting pydantic reject it first would answer
    a 422 whose body carries no code, which is the shape this contract exists to remove."""

    mode: str = ""


class PreferenceBody(BaseModel):
    fact: str = ""
    scope: str = "general"
    source: str = "you"


class CareMemberBody(BaseModel):
    """A new care-circle member. `may_receive` is required and may be empty on purpose: an empty
    list is "this person may receive nothing yet", which is a real thing to say about a clinic
    contact you have added but not decided about, and the store accepts it while clause 7 refuses
    to send them anything. There is no `added_by` field: the one value it may carry is written by
    the service, so a caller cannot propose a different one."""

    name: str = ""
    email: str = ""
    may_receive: list[str] = Field(default_factory=list)


class ProtectedFactBody(BaseModel):
    """A new protected fact. The value arrives here and is stored locally; it is never served back
    by any route, and `kind` is checked against a closed set rather than accepted as a string."""

    label: str = ""
    kind: str = ""
    value: str = ""


class PreferencePatch(BaseModel):
    fact: str = ""
    scope: str = ""


class DemoEmailBody(BaseModel):
    """One inbound email, as the visitor rewrote it.

    `body_text` is the attacker-controlled field and is deliberately NOT length-checked here: the
    route reads its body through `_read_capped` for the reason the playground does, so a declared
    `max_length` would be a limit that fires after the bytes are already in memory.

    `message_id` names WHICH seeded email is being edited. It is an id rather than a whole message
    because the planner reads the day to plan for and the conversation to reply into off the
    message's own `received_at` and `thread_id` — a visitor supplying those could ask about a day
    this fixture has no calendar for, and the demo would look broken rather than attacked."""

    message_id: str = ""
    body_text: str = ""


class IdentityBody(BaseModel):
    """The hash the owner is approving. Required, and it must be the one on the screen: an approval
    that could omit it would be an approval of whatever the artifact happens to say by the time the
    request arrives, which is the one thing an identity is for."""

    identity: str = ""


class TeachBody(BaseModel):
    """The rule the owner typed, in their own words.

    `str` rather than a constrained type on purpose: an over-long rule is refused by `teach.check_rule`
    with a reason that says how long it was and what to do about it, and a schema-level maximum would
    answer that with a validation error the owner cannot act on.
    """

    rule: str = ""


class AttackBody(BaseModel):
    """The email the judge wrote, verbatim, and nothing else.

    No subject, no sender, no options. Those are the harness's, not the caller's: an attack is the
    text of an email that arrived, and a body with a settable sender is a body that can attack the
    harness's own assumptions rather than the engine. The length cap is declared HERE as well as
    enforced in `playground.attack`, so a reader of the schema sees the bound and a caller that
    reached the model by a path that skipped the route is still bounded. The route reads the raw
    body itself (see `_attack_email`), which is what makes the bigger `MAX_REQUEST_BYTES` guard
    possible — a schema cannot refuse bytes it has already been handed.
    """

    email: str = Field(default="", max_length=playground.MAX_EMAIL_CHARS)


def _trust_proxy(explicit: bool | None = None) -> bool:
    """Whether to believe `X-Forwarded-For`. Read ONCE, when the router is built.

    Off by default, and the default is the whole finding. `X-Forwarded-For` is a header the caller
    writes, so a limit keyed on it is a limit any caller resets by editing a string — but behind a
    real reverse proxy the socket's peer address is the PROXY, and every caller shares one bucket,
    which lets one attacker spend everybody's budget. Neither answer is right for both
    deployments, so it is a switch, it is documented, and it is off.

    Read at build time rather than per request. A per-request read would put `os.environ` on the
    public path, which is the `.env` finding again with a different file name.
    """
    if explicit is not None:
        return bool(explicit)
    import os
    return (os.environ.get("PM_TRUST_PROXY", "") or "").strip().lower() in ("1", "true", "yes",
                                                                            "on")


def _trusted_proxy_hops(explicit: int | None = None) -> int:
    """How many trusted reverse proxies sit in front of this process, when `_trust_proxy` is on.

    `X-Forwarded-For` grows by one entry per hop, each proxy APPENDING the address it received the
    connection from, so the last `hops` entries are the ones this deployment's own trusted proxies
    wrote — the caller cannot touch them, because a caller no proxy relays the request from can
    never make it onto the wire at all. Everything to their LEFT is exactly as attacker-controlled
    as the whole header is when `_trust_proxy` is off: a caller sends whatever prefix it likes, and
    a proxy only ever appends, never rewrites, what arrived before it.

    Default 1 — the common single-reverse-proxy deployment (nginx, a cloud load balancer) — read
    from `PM_TRUST_PROXY_HOPS` the same way `_trust_proxy` reads its own switch: once, at build
    time. A value below 1, or one that will not parse as an integer, is treated as 1 rather than
    as the deployment's problem to notice at request time — an operator who mistypes the count
    should get the single-proxy default, not a client identity that trusts an unbounded prefix.
    """
    if explicit is not None:
        return explicit if explicit >= 1 else 1
    import os
    raw = os.environ.get("PM_TRUST_PROXY_HOPS", "")
    try:
        n = int(raw.strip())
    except ValueError:
        return 1
    return n if n >= 1 else 1


def _client(request: Request, trust_proxy: bool = False, trusted_hops: int = 1) -> str:
    """Who is asking, for the rate limit's purposes.

    The socket's own peer address, unless the deployment has said out loud that it is behind a
    proxy — see `_trust_proxy`. With the switch off this is the only identity a caller cannot
    forge, and it is the identity that is used.

    With the switch on, this used to take the FIRST hop of `X-Forwarded-For` — the entry the
    CALLER writes, since a client is free to prepend its own comma-separated chain before ever
    reaching the trusted proxy. `X-Forwarded-For: 1.2.3.4` from a caller with no proxy in front at
    all arrives at the trusted proxy, which appends the caller's real address and forwards
    `1.2.3.4, <real address>` — and the old code read `1.2.3.4`, a value the caller invented,
    minting a fresh rate-limit bucket on every request. The hop that identifies the caller is the
    one the trusted proxy chain ACTUALLY appended: counted back from the right by `trusted_hops`
    (see `_trusted_proxy_hops`), which for the default of one trusted proxy is the last entry.

    A header with fewer entries than `trusted_hops` is not a shorter chain to trust the front of —
    a correctly configured trusted proxy always appends, so a genuine request always carries at
    least `trusted_hops` entries — it is treated the same as a missing header: the peer address."""
    if trust_proxy:
        hops = [h.strip() for h in (request.headers.get("x-forwarded-for") or "").split(",")
               if h.strip()]
        if len(hops) >= trusted_hops:
            return hops[len(hops) - trusted_hops]
    return (request.client.host if request.client else "") or "unknown"


class PreferenceStore:
    """Consented facts, in memory, with their provenance.

    `source` is not decoration: a preference the owner typed and a preference this system inferred
    are different objects, and only one of them may be acted on without asking again. Everything
    written here is "you" until an inference path exists that can honestly say otherwise.

    The stored field is `fact`, and it is the field the contract names — a row that called it
    `text` internally and `fact` on the wire is one rename away from a client reading `undefined`
    and rendering a blank preference with no error anywhere."""

    def __init__(self, now=None) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._clock = now or (lambda: _dt.datetime.now().astimezone())

    def add(self, fact: str, scope: str = "general", source: str = "you") -> dict:
        row = {"id": uuid.uuid4().hex[:12], "fact": fact.strip(),
               "scope": (scope or "").strip() or "general",
               "source": (source or "").strip() or "you", "last_used": None}
        with self._lock:
            self._rows[row["id"]] = row
        return dict(row)

    def rows(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows.values()]

    def update(self, preference_id: str, fact: str, scope: str) -> dict | None:
        """Edit in place. The id, the consent `source` and `last_used` are NOT editable: they are
        the record of what was consented to and when it was last acted on, and a PATCH that could
        rewrite them would let an edit launder a fact into looking like it was always there."""
        with self._lock:
            row = self._rows.get(preference_id)
            if row is None:
                return None
            row["fact"], row["scope"] = fact.strip(), scope.strip() or "general"
            return dict(row)

    def delete(self, preference_id: str) -> bool:
        with self._lock:
            return self._rows.pop(preference_id, None) is not None


class V2Service:
    def __init__(self, *, engine, routine, boundary, changesets, executor, calendar,
                 mode: str = "sample", owner_name: str | None = None,
                 timezone: str | None = None, connections=None, preferences=None,
                 library=None, learner=None, now=None, authenticated_owner: bool = False,
                 owner_token: str | None = None, limits: LimitStore | None = None,
                 agreements: AgreementStore | None = None,
                 facts=None, care_circle=None, digests=None,
                 attacks: "playground.TokenBucket | None" = None,
                 demos: "object | None" = None) -> None:
        #: Concept D. Both may be None, which is the product without the learning loop — the same
        #: engine, one fewer destination. `GET /v2/skills` then answers an empty library rather
        #: than 404, because "you have learned nothing yet" is an answer and a 404 is a bug report.
        self.library = library
        self.learner = learner
        self.engine = engine
        self.routine = routine
        self.boundary = boundary
        self.changesets = changesets
        self.executor = executor
        self.calendar = calendar
        # "live" is a claim about what the providers ARE, so it is passed in by whoever wired the
        # connectors rather than guessed from the presence of an object.
        self.mode = mode
        self.owner_name = owner_name or getattr(engine, "owner_name", "Owner")
        self.timezone = timezone or getattr(engine, "timezone", "Europe/London")
        self.connections = [dict(c) for c in (connections or SAMPLE_CONNECTIONS)]
        self.preferences = preferences or PreferenceStore(now=now)
        self._clock = now or getattr(engine, "clock", None) or (
            lambda: _dt.datetime.now().astimezone())
        #: The credential the owner-only routes are gated on, or None for a build that has none.
        #:
        #: The third review's Critical is that this used to be a process-level boolean:
        #: `build_live(..., owner_session=True)` made EVERY request reaching the port the owner's,
        #: so a network caller could read the decision log and run the approve route. A boolean is
        #: not a credential — it is something the code says about itself, once, at startup, to
        #: nobody. The boundary is now a token the SERVER generates (see `owner_token`), presented
        #: by the caller, compared in constant time.
        #:
        #: `authenticated_owner` survives only as the SAMPLE's claim, and `require_owner` reads it
        #: only when `mode` is the sample. In live mode no boolean can open these routes: a live
        #: service with no token refuses everything, which is the fail-closed answer to a wiring
        #: mistake rather than a gate someone can talk their way past.
        #: `docs/API_V2.md` says approval must be enforced server-side, so the check is at the HTTP
        #: boundary rather than left to the browser to perform on itself.
        self.authenticated_owner = bool(authenticated_owner)
        self.owner_token = owner_token or None
        #: The owner's standing answer for this routine. Defaulted rather than required so a build
        #: that was never given one still serves the route honestly — with `ask_always`, which is
        #: the mode that acts on nothing.
        self.limits = limits if limits is not None else LimitStore(now=now)
        #: The playground's per-caller budget. Built with the service rather than at module scope
        #: so two services in one process — the sample and a live build in the same test run — do
        #: not share a bucket, and so a caller can hand in one of its own.
        self.attacks = attacks if attacks is not None else playground.TokenBucket()
        #: Every live demo session in this process, or None for a build that is not a demo. Handed
        #: in rather than built here because the manager owns the caps, the ledgers and the clock,
        #: and a service that built its own would be a second set of them to keep in step.
        #:
        #: Its presence is NOT what makes the demo routes reachable: `_demos` reads it together
        #: with `mode`, so a live build handed a manager still refuses — the same coupling `_build`
        #: uses for `authenticated_owner`, and for the same reason. A demo route is public in the
        #: router's table, so the only thing standing between a live service and a stranger asking
        #: it to start a world is that reading.
        self.demos = demos
        #: The agreements the owner is holding the other side to, and the one object allowed to
        #: re-read the world and compare. A build without a store serves an empty list rather than
        #: 404: "nothing has been agreed yet" is an answer, and a 404 is a bug report.
        self.agreement_store = agreements
        self._agreement_checker = (None if agreements is None
                                   else AgreementChecker(agreements, calendar, now=self._clock))
        #: The care scenario. All three optional, and each absence is answered rather than 404'd —
        #: "you have not added anyone yet" is an answer and a 404 is a bug report, the same reading
        #: `self.library` gets above. A build with no circle serves an empty list, and clause 7 then
        #: refuses every care action, which is the fail-closed direction for a missing list.
        self.facts = facts
        self.care_circle = care_circle
        self.digests = digests

    # ---- who is asking --------------------------------------------------------------------------
    def is_owner(self, presented: str | None) -> bool:
        """Whether this REQUEST is the owner's. Sample mode answers from its one explicit claim;
        every other mode answers only from the credential."""
        if self.owner_token is None:
            return self.mode == "sample" and self.authenticated_owner
        return matches(presented, self.owner_token)

    def require_owner(self, presented: str | None, *, code: str = "owner_forbidden") -> None:
        """Refuse unless the caller presented the owner's token.

        Named `require_` rather than `assert_` because it is the one thing standing between an
        untrusted caller and the approve route, and it refuses rather than returning a boolean so
        that forgetting the result is not a mistake the type system has to catch."""
        if self.is_owner(presented):
            return
        raise V2Error(403, "This route acts as the owner. Present the owner token — the file the "
                           "server wrote at startup — as `Authorization: Bearer <token>`.",
                      code=code)

    # ---- session and the watcher --------------------------------------------------------------
    def session(self, presented: str | None = None) -> dict:
        """The session, and whether THIS request is the owner's.

        It used to report a fact about the process, which is a different question and the one the
        web client was accidentally asking: it renders the owner's controls from this field, and a
        shared screen in front of the product is not the owner."""
        return {"mode": self.mode, "authenticated_owner": self.is_owner(presented),
                "owner_name": self.owner_name, "timezone": self.timezone,
                "connections": [dict(c) for c in self.connections],
                "routine": self.routine_state(), "boundary": self.boundary_state(),
                "changes": self.store_health()}

    def store_health(self) -> dict:
        """How many change records the store holds, and how many it could not read.

        The review's Medium finding is that an unreadable record used to be skipped in silence, so
        the owner saw a clean store that had quietly lost a change. A count on the session is the
        smallest honest surface for it: it travels with every screen, it needs no new endpoint, and
        a `corrupt` above zero is a fact the UI can refuse to look normal about."""
        store = getattr(self.engine, "store", None)
        if store is None or not hasattr(store, "counts"):
            return {"records": 0, "corrupt": 0, "loaded": 0}
        # `loaded` is the count the ENGINE is actually serving. `records` is what the file holds.
        # The third review's Low is that these can differ — the engine reads the store once, at
        # construction — and that nothing said so: a session could report a row count that its own
        # `changes` and `decisions` answers disagreed with. Reporting both does not reconcile them;
        # it makes the divergence a fact the owner and the UI can see rather than a silent one.
        return {**store.counts(), "loaded": len(self.engine.changes())}

    def routine_state(self) -> dict:
        """Only the three fields the contract names — the cursor and the interval are internals,
        and a UI that starts rendering them is a UI that has started depending on them."""
        state = self.routine.state()
        return {"enabled": bool(state.get("enabled", True)),
                "paused": bool(state.get("paused", False)),
                "last_checked_at": state.get("last_checked_at", "")}

    def set_routine(self, paused: bool, presented: str | None = None) -> dict:
        """Pause or resume the watcher. Owner-only: the watcher reads the owner's mailbox, and
        turning it back on is a decision about the owner's own machine."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        self.routine.pause() if paused else self.routine.resume()
        # The whole session, not the routine row: the screen that flipped the switch re-renders
        # from this response, and a partial body would leave it holding a stale session beside a
        # fresh one. The presentation is carried through, because the screen that just changed
        # something as the owner must not be told it is not the owner.
        return self.session(presented)

    def limits_state(self, presented: str | None = None) -> dict:
        """`GET /v2/limits` — the owner's standing answer and the clauses it covers.

        Owner-only, on the same rule as every other route that describes the owner's own rules.
        The clauses are the six sentences that decide whether this product may act unasked; that is
        a description of the owner's authority, which is not a thing to serve to whoever asks."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        return self.limits.state()

    def set_limits(self, mode: str, presented: str | None = None) -> dict:
        """`POST /v2/limits` — set the standing answer, and record that the owner set it.

        The audit row is not decoration. A mode change is the moment this product's authority to
        act unasked is widened or narrowed, and a widening that left no trace would be the one
        change to the safety posture that the audit chain could not answer for. The row carries the
        mode and the previous one, never anything about a change."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        previous = self.limits.mode()
        try:
            state = self.limits.set_mode(mode)
        except UnknownMode as exc:
            raise V2Error(422, str(exc), code=UNKNOWN_LIMITS_MODE)
        if state["mode"] != previous:
            self._audit("limits.mode_changed",
                        detail={"from": previous, "to": state["mode"]})
        return state

    def _audit(self, event: str, *, detail: dict) -> None:
        """One audit write, best-effort. A build with no audit chain is a build that has nothing to
        write to, which is not a reason to refuse the owner's setting — the mode is already stored,
        and a 500 here would tell the caller the opposite of what happened."""
        audit = getattr(self.engine, "audit", None)
        if audit is None:
            return
        try:
            audit.record(event, detail=detail)
        except Exception:                                            # noqa: BLE001
            pass

    def commitment(self):
        """The protected commitment, or None. `None` is a real state — a build with no boundary
        configured — and the session reports it as `configured: false` rather than inventing a
        window the planner is not using."""
        try:
            return self.engine.commitment()
        except LookupError:
            return None

    def boundary_state(self) -> dict:
        """`docs/API_V2.md`: `{start: "HH:MM", buffer_minutes, configured}`.

        `start` is the clock time of the protected window, not the instant: the session screen
        renders it into an `<input type="time">`, and an ISO date there would be a value the
        browser silently blanks."""
        current = self.commitment()
        if current is None:
            return {"start": "", "buffer_minutes": 0, "configured": False}
        return {"start": str(current.start)[11:16], "buffer_minutes": int(current.buffer_minutes),
                "configured": True}

    def set_boundary(self, start: str, buffer_minutes: int,
                     presented: str | None = None) -> dict:
        """Move the protected window, and let the movement invalidate what was approved against it.

        Nothing is re-approved here. `BoundaryStore.put` writes the NEXT version, and every
        approval is a compare-and-swap on the version it was reviewed against — so a plan built
        against the old window stops being approvable the moment this returns, which is the only
        place that has to be true."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if not _HHMM.match(str(start or "")):
            raise V2Error(422, "Choose a start time between 00:00 and 23:59.",
                          code="invalid_boundary")
        if not isinstance(buffer_minutes, int) or isinstance(buffer_minutes, bool) or \
                not 0 <= buffer_minutes <= MAX_BUFFER_MINUTES:
            raise V2Error(422, f"Choose a buffer between 0 and {MAX_BUFFER_MINUTES} minutes.",
                          code="invalid_boundary")
        current = self.commitment()
        if current is None:
            raise V2Error(409, "There is no protected commitment to move. Connect a calendar "
                               "first.", code="connection_required")
        old_start, old_end = _moment(current.start), _moment(current.end)
        new_start = old_start.replace(hour=int(start[:2]), minute=int(start[3:5]),
                                      second=0, microsecond=0)
        # The LENGTH is preserved: the owner is moving a commitment, not restating how long it
        # takes, and a boundary edit that quietly shortened the school run would be a privacy
        # control that changed the meaning of the thing it protects.
        self.boundary.put(id=current.id, start=new_start.isoformat(),
                          end=(new_start + (old_end - old_start)).isoformat(),
                          buffer_minutes=int(buffer_minutes), title=current.title,
                          location=current.location, reason=current.reason,
                          attendees=current.attendees)
        return self.session(presented)

    def connect(self, service: str, presented: str | None = None) -> dict:
        """Attach a service, in the sample without asking anyone.

        Owner-only, and the reason is the ROW rather than the provider call: this marks the
        connection `connected`, and the session screen renders authorization state from it. An
        anonymous caller who could set that row could make the UI say Gmail is connected when no
        owner ever consented — a false statement about consent, produced by a stranger. In the
        sample the gate is open by construction (`is_owner` answers from the sample's own claim),
        so the demo still connects with one click.

        A live engine must run its authorized flow and confirm the provider said yes; the only
        thing this method is allowed to know is that the row moved, which is why the detail is
        carried over rather than rewritten into a claim about a token."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if service not in SERVICES:
            raise V2Error(404, f"no service '{service}'; this build connects "
                               f"{', '.join(SERVICES)}", code="not_found")
        for row in self.connections:
            if row["service"] == service:
                row["state"] = "connected"
        # The presentation is carried through: the screen that just connected something as the
        # owner must not be handed a session saying it is not the owner.
        return self.session(presented)

    # ---- the day ------------------------------------------------------------------------------
    def today(self, date: str | None = None) -> dict:
        """The day, defaulting to the service's own.

        `?date` omitted is how the browser asks for "today" — `api.today()` is called with no
        argument on first load, and a required parameter made that a 422, so the workspace never
        rendered at all."""
        day, offset = _day(date, self._clock())
        try:
            events = self.calendar.list_events(f"{day}T00:00:00{offset}",
                                               f"{day}T23:59:59{offset}")
        except ReauthRequired as exc:
            raise _reconnect(exc)
        return {
            "date": day, "timezone": self.timezone,
            "events": [{"id": e.id, "title": e.summary, "start": e.start, "end": e.end,
                        "kind": "event"} for e in events if str(e.start)[:10] == day],
            # The protected block is four times and a label. Its title, location, reason and
            # attendees are not in this response, and are not in the object it was built from.
            "protected": [{"id": c.id, "start": c.start, "end": c.end,
                           "buffer_minutes": c.buffer_minutes, "label": "Protected"}
                          for c in self.boundary.current_all() if str(c.start)[:10] == day],
            # Open changes first, in the order they arrived. `web/app/page.tsx` renders
            # `changes[0]` as the thing to handle — that is what "needs you" means — and a day
            # that holds a finished change and a new one would otherwise answer in arrival order,
            # dropping a reload onto the finished one with nothing left to do on it.
            "changes": [self._summary(c) for c in sorted(
                (c for c in self.engine.changes() if _belongs_to_day(c, day)),
                key=lambda c: c.stage in _SETTLED)],
        }

    def _summary(self, change) -> dict:
        return {"id": change.id, "from_name": change.from_name,
                "from_address": change.from_address, "received_at": change.received_at,
                "excerpt": change.excerpt, "thread_url": _thread_url(change),
                "stage": change.stage}

    # ---- one change ---------------------------------------------------------------------------
    def change(self, change_id: str) -> dict:
        try:
            return self.engine.change_dict(change_id)
        except KeyError:
            raise _missing(change_id)

    def prepare(self, change_id: str, presented: str | None = None) -> dict:
        """Prepare, and answer with the change itself.

        The UI does `setChange(await api.prepare(id))`: the returned object IS the screen. A
        `{change_id, stage}` receipt is not a smaller answer, it is a blank one — the review
        screen renders `proposal.actions`, `proposal.venues` and `proposal.protected`, and none of
        them were in it.

        The two refusals before the engine runs are the contract's, not the engine's: a paused
        routine and a missing connection are states of the WORKSPACE, and the engine is handed
        ports and cannot tell whether the port it got is an authorized one.

        Owner-only, and enumerated with the rest: preparing reads the owner's mailbox and spends
        the owner's model budget. A plan nobody asked for is a bill nobody agreed to."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        self._require_ready(change_id)
        try:
            self.engine.prepare(change_id)
        except KeyError:
            raise _missing(change_id)
        except EgressRefused as exc:
            raise _refused(exc)
        except ReauthRequired as exc:
            raise _reconnect(exc)
        except LookupError as exc:
            raise V2Error(409, str(exc), code="invalid_stage")
        # AFTER the plan is committed, never before: the standing limits decide whether a COMPLETE
        # bundle may run, so there is nothing to check until one exists. A preparation that ended
        # in `no_safe_fit` has no bundle and is simply a change waiting for the owner, which is
        # also what a preparation ends as whenever a clause refuses.
        self._run_within_limits(change_id)
        return self.change(change_id)

    def _run_within_limits(self, change_id: str) -> bool:
        """Run a prepared change without asking, if the owner's standing limits cover all of it.

        Returns whether it ran. Everything that is not a clean, in-limit run leaves the change
        exactly where `prepare` left it — a plan waiting for the owner — which is the fallback the
        preset promises: outside the limit, the product behaves as `ask_always` does.

        The order here is the safety argument. The clauses are checked FIRST, by trusted code, and
        only a review with no refusal reaches the execution below. The bundle is then admitted the
        same way an owner's approval admits it — a fresh read of the invite and of the boundary
        version, compared against what the bundle was built against — so a limit run is not a
        weaker path to the provider, it is the same path with a different thing standing where the
        owner stands.
        """
        if self.limits.mode() != limit_rules.WITHIN_LIMITS:
            return False
        # Nothing to review is not a review. A preparation that ended in `no_safe_fit` — or any
        # other ending with no bundle — has no actions the limits could permit or refuse, and six
        # clause rows about a plan that does not exist would be six rows of noise in the owner's
        # log, all of them counting toward the change's blocked attempts. The change is simply one
        # the owner is asked about, which is what it already was.
        cs = self.changesets.for_change(change_id)
        if cs is None:
            return False
        try:
            review = self.engine.limit_review(change_id)
        except KeyError:
            raise _missing(change_id)
        self.engine.record_limit_review(change_id, review)
        if not review.in_limit:
            # The failed clause is on the log, in the owner's own words, under `refused`. Nothing
            # else is written and nothing runs.
            return False
        try:
            event = self.calendar.get_event(cs.calendar.arguments["event_id"])
            boundary_version = self.boundary.version_of(cs.boundary_id)
        except (KeyError, EtagMismatch, ReauthRequired) as exc:
            return self._hand_back(change_id, cs, f"the limit run could not start: {exc}")
        # ONE transaction from here to the approval. The mode is read AGAIN inside it, and the
        # grant is written by the same statement that sets `approved`, so "the limits were still
        # `within_limits`" and "this version is approved" are one ordered write rather than two
        # calls that happened to be adjacent. The review's High is what the gap between them was:
        # an owner setting `off` in it left an already-authorized run to execute anyway.
        try:
            with self.changesets.transaction():
                revision = self.limits.revision()
                if self.limits.mode() != limit_rules.WITHIN_LIMITS:
                    # Read here, decided here, recorded by the hand-back below.
                    changed = True
                else:
                    changed = False
                    grant = self.engine.limit_grant(change_id, cs, review,
                                                    mode_revision=revision)
                    self.changesets.approve(cs.id, cs.version, current_event_etag=event.etag,
                                            current_boundary_version=boundary_version,
                                            limit_grant=grant)
        except (StaleChangeSet, KeyError, EtagMismatch, ReauthRequired) as exc:
            # The world moved between the check above and the admission. Nothing has run, so the
            # change goes back to being one the owner is asked about — which is the honest
            # fallback, and better than a limit run that failed halfway.
            return self._hand_back(change_id, cs, f"the limit run could not start: {exc}")
        if changed:
            return self._hand_back(
                change_id, cs,
                "the standing limits changed while this run was being authorized; nothing was sent")
        try:
            outcome = self.executor.execute(cs.id, version=cs.version)
        except BaseException as exc:                                 # noqa: BLE001
            # Pre-effect and post-effect failures are different facts and this used to report both
            # as "nothing happened". `_hand_back` only puts the bundle back when the executor never
            # moved it off `approved`; the decision row has to say the same thing, because a change
            # whose invite already moved is not a change that is waiting to be asked about.
            return self._hand_back(change_id, cs, f"the limit run did not complete: {exc}",
                                   partial=self._started(cs))
        self.engine.mark_executed_by(change_id, "limit")
        self._settle(change_id, outcome)
        return True

    def _started(self, cs) -> bool:
        """Whether the executor got past admitting the bundle — i.e. whether any effect may have
        been attempted. Read from the store rather than from the exception, because the question is
        about the bundle's recorded state and not about how the failure was raised."""
        current = self.changesets.get(cs.id, cs.version)
        return current is not None and current.status != APPROVED_STATUS

    def _hand_back(self, change_id: str, cs, reason: str, *, partial: bool = False) -> bool:
        """Put a bundle the limit run admitted but could not execute back where the owner can act.

        Only when the executor never reached the point of touching a provider: the bundle is still
        `approved` and no effect has been attempted. A bundle the executor got as far as `executing`
        is left exactly as it is, because "nothing ran" would be a guess and this is the one place
        a guess is not allowed. Either way the reason is a decision row, so the owner sees a change
        that stopped rather than a change that silently did nothing.

        `partial` is that distinction, stated rather than inferred by the reader: the run reached at
        least one effect and the row says so. The review's Low is that both used to arrive as "the
        limit run did not complete", which reads as "nothing happened" over a meeting that may
        already have moved.
        """
        current = self.changesets.get(cs.id, cs.version)
        retreated = current is not None and current.status == APPROVED_STATUS
        if retreated:
            self.changesets.mark(cs.id, cs.version, PENDING_STATUS)
        if partial and not retreated:
            self.engine.note_execution_partial(change_id, reason)
            # Stamped, because a run that reached an effect is a run the limits did, whatever it
            # achieved — and "who did this" must not read as "nobody" over a moved meeting.
            self.engine.mark_executed_by(change_id, "limit")
            self.engine.settle(change_id, PARTIAL)
        else:
            self.engine.note_execution_refused(change_id, reason)
        return False

    def _require_ready(self, change_id: str) -> None:
        """Refuse preparation the workspace cannot support, in the order the owner would fix it."""
        try:
            self.engine.require(change_id)
        except KeyError:
            raise _missing(change_id)
        if self.routine_state()["paused"]:
            raise V2Error(409, "Resume the routine before preparing this change.",
                          code="routine_paused")
        for row in self.connections:
            if row.get("state") != "connected":
                raise V2Error(409, f"Reconnect {row['service']} before preparing this change.",
                              code="connection_required")

    def leave(self, change_id: str, presented: str | None = None) -> dict:
        """Leave the meeting unchanged. Any approval raised for it is spent, not left lying.

        Owner-only: it settles the owner's plan, and it denies the approvals that were raised for
        it — an unauthenticated caller could otherwise cancel a plan the owner was about to
        approve."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        try:
            self.engine.leave(change_id)
        except KeyError:
            raise _missing(change_id)
        return self.change(change_id)

    def edit(self, change_id: str, body: EditBody, presented: str | None = None) -> dict:
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        try:
            self.engine.revise(change_id, reply_body=body.reply_body, start=body.start,
                               venue_index=body.venue_index)
        except KeyError:
            raise _missing(change_id)
        except NoFit as exc:
            # The day said no. The reason names the numbers, which is the whole point of it.
            raise V2Error(422, exc.reason, code="boundary_conflict")
        except EgressRefused as exc:
            raise _refused(exc)
        except ReauthRequired as exc:
            raise _reconnect(exc)
        except ValueError as exc:
            # A time that cannot be read, or a reply that is empty: the edit is the thing that is
            # wrong, and saying so is better than a 500 the UI cannot explain.
            raise V2Error(422, str(exc), code="boundary_conflict")
        except LookupError as exc:
            raise V2Error(409, str(exc), code="invalid_stage")
        return self.change(change_id)

    def approve(self, change_id: str, version: int, presented: str | None = None) -> dict:
        """Approve exactly this version, run it, and answer with the change.

        The UI sets the returned object as the screen AND then asks for the outcome; a body of
        `{effects, stage}` gave it neither a change to render nor the summary line."""
        self.require_owner(presented, code=APPROVAL_FORBIDDEN)
        # `off` refuses an approval rather than swallowing it. A preset that silently discarded a
        # fresh, specific "yes" would leave the review screen reading "approved" over a meeting
        # that never moved, which is the failure this product exists to not have; a refusal the
        # owner can read is loud, and loud is recoverable.
        if self.limits.mode() == limit_rules.OFF:
            raise V2Error(409, limit_rules.OFF_MEANS, code=LIMITS_OFF)
        cs = self.changesets.for_change(change_id)
        if cs is None:
            raise _missing(change_id)
        try:
            event = self.calendar.get_event(cs.calendar.arguments["event_id"])
        except KeyError:
            raise V2Error(409, "the invite this was built against no longer exists",
                          code="stale_version")
        except ReauthRequired as exc:
            raise _reconnect(exc)
        except EtagMismatch as exc:
            raise _stale(exc)
        try:
            # Fresh reads, compared against what the bundle was BUILT against. The approval is a
            # compare-and-swap on the world, not a flag the request can set.
            self.changesets.approve(cs.id, version, current_event_etag=event.etag,
                                    current_boundary_version=self.boundary.version_of(
                                        cs.boundary_id))
        except StaleChangeSet as exc:
            raise _stale(exc)
        try:
            outcome = self.executor.execute(cs.id, version=version)
        except StaleChangeSet as exc:
            raise _stale(exc)
        # Stamped after the run, and only when the run happened: "the owner did this" is a claim
        # about an execution, and a stamp written before the effects would survive a failure that
        # left them undone.
        self.engine.mark_executed_by(change_id, "owner")
        self._settle(change_id, outcome)
        return self.change(change_id)

    def _settle(self, change_id: str, outcome) -> None:
        """Move the change to the stage its effects actually reached. A partial run is not done.

        The third review's first High is that this used to assign the stage and stamp the time in
        memory, and stop there: the change-set row carried the outcome while the CHANGE record
        still said `proposal_ready`, so a restart re-offered an executed plan as one waiting for
        the owner. The move is the engine's now, so it goes through the same write every other
        stage transition does."""
        states = [e.state for e in outcome.effects]
        if all(s == "done" for s in states):
            stage = DONE
        elif any(s == "done" for s in states) or any(s == "unknown" for s in states):
            # "unknown" is not "failed": something may well have landed, so the change is not
            # quietly closed as a failure the owner can forget about.
            stage = PARTIAL
        elif states and all(s == "failed" for s in states):
            stage = STOPPED
        else:
            return
        self.engine.settle(change_id, stage)

    def decisions(self, change_id: str, presented: str | None = None) -> list:
        """The ordered allow/refuse record for one change.

        A bare list, because the contract says a bare list and because a UI that has to unwrap an
        envelope to find the rows is a UI that has to handle the envelope being absent. An unknown
        change is a 404: "no decisions were recorded" and "no such change" are different answers
        and a client that cannot tell them apart cannot tell a bug from a refusal.

        Owner-only, under the same rule `approve` applies and for the same reason. This is not
        scrubbed into anonymity: a row names what the engine checked, under which clause, against
        which surface, and what it refused — workflow metadata that describes the owner's own
        private commitments even when the protected text itself never appears. The screen it was
        built for is the owner's review screen; a caller who is not the owner has no business
        reading it. This build has one credential and it is not a login: the owner token the server
        minted into its own file, presented as `Authorization: Bearer`. That is the whole of the
        boundary — which is exactly why it has to be checked here rather than left to the browser."""
        self.require_owner(presented, code=DECISIONS_FORBIDDEN)
        try:
            self.engine.require(change_id)
        except KeyError:
            raise _missing(change_id)
        return list(self.engine.decisions_for(change_id))

    def model_view(self, change_id: str, presented: str | None = None) -> dict:
        """Every prompt this change sent to a model, and everything that never left the machine.

        Gated exactly as `/decisions` is, and for the same reason rather than by analogy: this
        route returns the prompts VERBATIM — that is what makes it evidence — so a caller who is
        not the owner would be reading whatever the change's planner was allowed to see. The gate
        is the first statement in the body, before the change is even looked up, so an unauthorised
        caller cannot use the 404 to probe which change ids exist.

        A 404 for an unknown change, matching `/decisions`: "this change sent nothing" and "there
        is no such change" are different answers and the receipt list is only meaningful about a
        change that exists.

        In the sample the route is open — there is no token to present and the data is synthetic —
        and the response is put through the privacy gate anyway. "The data is fake" is a claim about
        the fixtures, not about this code path: the second it stops being true, the redaction is
        already in the way, rather than being a change someone has to remember to make. The gate is
        applied per call and per prompt, so a call whose `prompt_sent` carries a protected value
        loses that field and gains the refusal that explains it, instead of the whole view being
        withheld."""
        self.require_owner(presented, code=DECISIONS_FORBIDDEN)
        try:
            self.engine.require(change_id)
        except KeyError:
            raise _missing(change_id)
        view = self.engine.model_view(change_id)
        # IN BOTH MODES. The sample-only condition was the finding: a live build's model-view is
        # served from the same audit rows, and "the sample is synthetic" is a claim about the SAMPLE,
        # not a reason to stop checking what a live build put in a prompt and wrote down. The gate
        # is the engine's, so the care facts are in scope wherever the build has them.
        return _gate_prompts(view, self.boundary, gate=getattr(self.engine, "gate", None))

    # ---- the playground: the judge writes the attack ---------------------------------------------
    def attack(self, email: str, presented: str | None = None, client: str = "") -> dict:
        """Run one judge-written attack through the real sample engine.

        Sample mode only, and a 404 rather than a 403 in live mode: the playground is not a
        restricted capability, it is a capability that does not exist off the sample. A live build
        has real providers behind its ports, and the honest answer to "attack it for me" there is
        that this route is not part of the product — the corpus in `redteam/` is.

        Not owner-gated, deliberately. The whole point of the endpoint is that a judge can run it
        without being handed the owner token, and the thing it can reach is a sample world built
        for the request — no owner data, no network, no credential. The rate limit and the CPU cap
        are what stand in for the gate, because the resource being protected is this process.

        The three failures are three different answers and none of them is a verdict: 422 for an
        email past the cap (nothing ran), 503 for a run that spent its CPU budget (nothing about it
        is a result), 429 for a caller past its rate (nothing to do with this attack).

        The token is spent LAST, after the mode and the email have both been accepted. It used to
        be spent first, which made ten 2,001-character bodies a way to lock the next judge out of
        the demo without ever running the engine — a refusal that costs the caller nothing and the
        service a minute of one caller's quota is a denial-of-service primitive wearing a limit's
        clothes. Every check that can refuse a request now runs before the one that has a price.
        The route has already enforced the byte cap and the character cap by the time this is
        called; the length check below stays as the library's own guard rather than as the route's.
        """
        if self.mode != "sample":
            raise V2Error(404, "the playground is not available in live mode", code="not_found")
        if len(email) > playground.MAX_EMAIL_CHARS:
            raise V2Error(422, f"the email is {len(email)} characters; the limit is "
                               f"{playground.MAX_EMAIL_CHARS}", code="playground_limit")
        if not self.attacks.take(client or "unknown"):
            raise V2Error(429, f"the playground runs {playground.RATE_LIMIT_REQUESTS} attacks a "
                               f"minute per caller; wait and try again.", code="rate_limited")
        try:
            return playground.attack(email)
        except playground.EmailTooLong as exc:
            raise V2Error(422, str(exc), code="playground_limit")
        except playground.CpuBudgetExceeded as exc:
            raise V2Error(503, str(exc), code="playground_limit")

    # ---- the public demo: a real model, simulated providers, capped per visitor -----------------
    def _demos(self):
        """The session manager, or the refusal that says this build has no demo in it.

        A 404 rather than a 403, for the reason `attack` gives: a demo session is not a restricted
        capability, it is a capability that does not exist off demo mode. A live build has real
        providers behind its ports and a real owner's data behind its stores, and "start a
        quarantined world for a stranger" is not a thing it should offer."""
        if self.mode != demo_session.DEMO_MODE or self.demos is None:
            raise V2Error(404, "this build has no demo: it is not in demo mode", code="not_found")
        return self.demos

    def _demo_session(self, session_id: str):
        try:
            return self._demos().get(session_id)
        except KeyError:
            raise V2Error(404, f"no demo session '{session_id}' — it may have expired",
                          code="not_found")

    def demo_start(self, client: str) -> dict:
        """Begin a visitor's session and answer with everything a screen needs to be honest.

        Not owner-gated, deliberately, and this is the second route to earn that: what it reaches
        is a world built for the request, over `:memory:` stores, with simulated providers. The
        resource it puts at risk is this process's memory, which is why `DemoSessions` bounds the
        session count and the TTL, and why the answer carries the caps.

        Session creation is RATED like any other request — `DemoSessions.start` spends the same
        per-client bucket a model call does — because without it "start a session" was the one
        thing in the demo a visitor could do in an unrated loop, allocating toward `max_sessions`
        for free. `DemoRateLimited` is a 429, distinct from `DemoRefused`'s 503: one names a state
        THIS caller resolves by waiting, the other a state every caller shares."""
        try:
            session = self._demos().start(client or "unknown")
        except demo_session.DemoRateLimited as exc:
            raise V2Error(429, str(exc), code="rate_limited")
        except demo_session.DemoRefused as exc:
            # 503 with a sentence: the demo is at its session limit or has no model key. Both are
            # "not now" rather than "you did it wrong", and a judge should read the sentence.
            raise V2Error(503, str(exc), code="demo_unavailable")
        return session.state()

    def demo_state(self, session_id: str) -> dict:
        """Budget left, calls left, the seeded inbox, and what is real versus simulated."""
        return self._demo_session(session_id).state()

    def demo_email(self, session_id: str, body_text: str, message_id: str) -> dict:
        """Rewrite one inbound email, run the real pipeline, and answer with what happened.

        This is the demo's whole point: the visitor writes the injection and reads the model's
        ACTUAL output and the decision the trusted code made about it — not a scripted verdict
        about a scripted attack.

        A 422 for an empty body (nothing ran, and running the pipeline on nothing would burn a
        model call to produce a confusing change), 404 for an unknown message or session, and a
        422 for a body past `playground.MAX_EMAIL_CHARS` — the SAME 2,000-character bound the
        playground's `attack` route holds callers to, reused rather than re-picked: `body_text`
        reaches `changes._extract`'s prompt WHOLE, so an unbounded body is an unbounded multiple
        of the measured per-run cost `docs/API_V2.md` and the README's cost table are written
        against (an ~8KB body was measured at several times `$0.001453`/run). Refused, not
        truncated — a silently shortened body is a different injection than the one the visitor
        wrote, and a demo that quietly rewrites what a visitor sent to make it fit is a worse
        demo than one that says no. Every check runs before the one with a price, the same
        ordering `attack` documents."""
        session = self._demo_session(session_id)
        if not body_text.strip():
            raise V2Error(422, "the email body is empty — nothing to run", code="demo_empty_email")
        if len(body_text) > playground.MAX_EMAIL_CHARS:
            raise V2Error(422, f"the email is {len(body_text)} characters; the limit is "
                               f"{playground.MAX_EMAIL_CHARS}", code="demo_limit")
        try:
            return session.run_email(message_id=message_id, body_text=body_text)
        except KeyError:
            raise V2Error(404, f"this session's inbox has no message '{message_id}'",
                          code="not_found")

    def demo_email_compromised(self, session_id: str, body_text: str, message_id: str) -> dict:
        """The other half of the demo's claim: the SAME email, the SAME engine and gate, a model
        that complies with everything the email asks instead of the real one.

        Free by construction — see `DemoSession.run_email_compromised` — so it is checked the same
        way `demo_email` is checked (empty body, the same length cap) but never reaches
        `SessionBudget.take_call()`: nothing here spends the session's model budget or the demo's,
        and `docs/API_V2.md` says so. `DemoRateLimited` still applies, because the ROUTE is a
        thing a caller could hammer even though the run underneath it is free."""
        session = self._demo_session(session_id)
        if not body_text.strip():
            raise V2Error(422, "the email body is empty — nothing to run", code="demo_empty_email")
        if len(body_text) > playground.MAX_EMAIL_CHARS:
            raise V2Error(422, f"the email is {len(body_text)} characters; the limit is "
                               f"{playground.MAX_EMAIL_CHARS}", code="demo_limit")
        try:
            return session.run_email_compromised(message_id=message_id, body_text=body_text)
        except KeyError:
            raise V2Error(404, f"this session's inbox has no message '{message_id}'",
                          code="not_found")
        except demo_session.DemoRateLimited as exc:
            raise V2Error(429, str(exc), code="rate_limited")

    def outcome(self, change_id: str) -> dict:
        try:
            change = self.engine.require(change_id)
        except KeyError:
            raise _missing(change_id)
        cs = self.changesets.for_change(change_id)
        if cs is not None and cs.outcome:
            out = dict(cs.outcome)
        else:
            # Nothing has run: pending is the true state, and it is a state the UI can render.
            out = {"effects": [{"kind": kind, "state": "pending", "detail": "not started"}
                               for kind in (CALENDAR_UPDATE_KIND, EMAIL_REPLY_KIND)],
                   "summary": ""}
        # The contract puts the overreach event on the outcome as well as on `/why`: an effect that
        # completed is not evidence that the code that shaped it behaved.
        out["overreach"] = list(change.overreach)
        out["learned_skill"] = change.skill
        return out

    def why(self, change_id: str) -> dict:
        try:
            return self.engine.why(change_id)
        except KeyError:
            raise _missing(change_id)

    def plans(self) -> list[dict]:
        """A bare list, exactly as `docs/API_V2.md` writes it.

        `/skills` was already a bare list and `/plans` wrapped — a client cannot guess which
        routes wrap, so it either wraps everything (and breaks on skills) or unwraps everything
        (and breaks on plans). The contract settles it; this follows."""
        return self.engine.plans()

    # ---- agreements ---------------------------------------------------------------------------
    def agreements(self, presented: str | None = None) -> dict:
        """What the owner agreed to, with the last answer anyone got about it.

        Reads the STORE and touches no provider: the state served here is the state the last check
        left behind, and this route must be safe to poll — a route that re-read the calendar on
        every render would turn opening a tab into a provider cost, and would let the answer change
        under a client that only asked to look. `POST /v2/agreements/check` is the one that looks.

        `rows()` and not `list()`: a stored record this build cannot parse is served as an
        `unchecked` row with a note, rather than dropped. The review's Medium is that a corrupt
        payload turned the owner's list into an empty one — the one answer that looks exactly like
        "you have no agreements" while the record is still on disk. `docs/API_V2.md` states it.
        """
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.agreement_store is None:
            return {"agreements": []}
        return {"agreements": [a.as_dict() for a in self.agreement_store.rows()]}

    def check_agreements(self, presented: str | None = None) -> dict:
        """Re-read every agreed event and report what still matches.

        Owner-gated like the other owner routes even though it writes nothing outward: it reads the
        owner's calendar, and "who may ask this product to go and look at your meetings" is a
        question about the owner, not about whether the call is harmless.

        Nothing here acts on a difference. The comparison's whole output is the response.
        """
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self._agreement_checker is None:
            return {"agreements": []}
        self._agreement_checker.check()
        return self.agreements(presented)

    # ---- the care circle ----------------------------------------------------------------------
    def care_circle_state(self, presented: str | None = None) -> dict:
        """Who may receive what. Owner-gated, and it is the one care route that is.

        The circle is the owner's own list of the people in their parent's life — names, addresses,
        and what each of them is allowed to be told. A read of it is a read of the family, so it is
        the owner's for the same reason the decision log is: not because the call has a side effect,
        but because the answer is about them.
        """
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        members = self.care_circle.members() if self.care_circle is not None else []
        return {"members": [m.as_dict() for m in members]}

    def add_care_member(self, name: str, email: str, may_receive, presented: str | None = None):
        """Add a member. THE OWNER ONLY, and the store enforces it a second time.

        `by=care.OWNER` is written here because this method has already proved the caller is the
        owner; the store checks the string again because the route is not the only possible caller.
        Two checks for one rule is not redundancy when the second one is the one that survives a
        new call path being added later.
        """
        from . import care as care_rules

        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.care_circle is None:
            raise V2Error(409, "This build has no care circle to add anyone to.",
                          code="care_unavailable")
        try:
            member = self.care_circle.add(name=name, email=email, may_receive=may_receive,
                                          by=care_rules.OWNER)
        except (care_rules.CareRefused, care_rules.UnknownKind) as exc:
            raise _care_refusal(exc) from exc
        return member.as_dict()

    def remove_care_member(self, member_id: str, presented: str | None = None) -> None:
        from . import care as care_rules

        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.care_circle is None:
            raise V2Error(409, "This build has no care circle.", code="care_unavailable")
        if not self.care_circle.remove(member_id, by=care_rules.OWNER):
            raise V2Error(404, f"No care-circle member '{member_id}'.", code="care_member_missing")

    def send_digest(self, day: str | None = None, presented: str | None = None) -> dict:
        """Send the day's digest to the care circle. OWNER ONLY, and the audience is not a parameter.

        Finding 11 was that `digest_email` existed and nothing in the product called it, so the
        once-a-day cap in clause 7 was a clause about a table nothing wrote to. This is the route
        that makes it real, and the shape of it is the whole answer to findings 12 and 13:

        * The DAY is the only thing a caller supplies, and it is defaulted and checked. An empty day
          is refused by the ledger rather than treated as a bucket every day falls into, and a day
          the caller invents is a day with no digest in the ledger — which is exactly the state a
          first digest for that day is in, so the cap still holds and the artifact still names the
          day it was composed for.
        * The AUDIENCE is read from the circle inside `build_digest` and can be named by nobody on
          the wire. A caller who could name the recipients could name one the owner never listed,
          which is the disclosure this feature exists to prevent arriving through its own
          announcement.
        * Both the membership and the grant are taken from ONE snapshot at send time, so a member
          removed between the check and the port cannot be sent to by a check that ran first.

        A refusal here is a policy refusal, not a crash: the care exceptions are mapped to 422 by
        `_care_refusal`, whose body names the mistake and never the value.
        """
        from . import care as care_rules

        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.executor is None:
            raise V2Error(409, "This build has no executor, so nothing can be sent.",
                          code="care_unavailable")
        day, _offset = _day(day, self._clock())
        try:
            plan = self.executor.build_digest(day, now=self._clock().isoformat(timespec="seconds"))
            return self.executor.send_digest(plan)
        except (care_rules.CareRefused, care_rules.DigestDayMissing,
                care_rules.DigestAlreadySent) as exc:
            raise _care_refusal(exc) from exc
        except CareAudienceRefused as exc:
            raise _care_refusal(exc) from exc

    # ---- protected facts ----------------------------------------------------------------------
    def protected_facts_state(self, presented: str | None = None) -> list[dict]:
        """The owner's list of what is private — the LABELS, never the values.

        The route answers `{id, label, kind, in_use}`, and there is deliberately no endpoint, no
        query parameter and no debug flag that returns a value. A screen can therefore show the
        owner their own list, and the list itself never becomes a readable copy of the things the
        gate exists to keep in — which would be a disclosure route wearing a settings page.
        """
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        facts = self.facts.facts() if self.facts is not None else []
        return [f.as_dict() for f in facts]

    def add_protected_fact(self, label: str, kind: str, value: str, presented: str | None = None):
        from . import care as care_rules

        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.facts is None:
            raise V2Error(409, "This build has no protected-fact store.",
                          code="care_unavailable")
        try:
            fact = self.facts.add(label=label, kind=kind, value=value, by=care_rules.OWNER)
        except (care_rules.CareRefused, care_rules.UnknownKind) as exc:
            raise _care_refusal(exc) from exc
        # `as_dict` and not the record: the response to "here is a secret I am storing" is not the
        # secret. Echoing it back would put it in the browser's memory, the access log and whatever
        # the caller does with a response body — three copies of the thing just declared private.
        return fact.as_dict()

    def remove_protected_fact(self, fact_id: str, presented: str | None = None) -> None:
        from . import care as care_rules

        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.facts is None:
            raise V2Error(409, "This build has no protected-fact store.", code="care_unavailable")
        if not self.facts.remove(fact_id, by=care_rules.OWNER):
            raise V2Error(404, f"No protected fact '{fact_id}'.", code="protected_fact_missing")

    # ---- preferences --------------------------------------------------------------------------
    def list_preferences(self) -> list[dict]:
        return self.preferences.rows()

    def add_preference(self, body: PreferenceBody, presented: str | None = None) -> dict:
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        fact, scope = body.fact.strip(), body.scope.strip()
        if not fact or not scope:
            raise V2Error(422, "Fact and scope cannot be empty.", code="invalid_preference")
        return self.preferences.add(fact, scope, body.source)

    def update_preference(self, preference_id: str, body: PreferencePatch,
                          presented: str | None = None) -> dict:
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        fact, scope = body.fact.strip(), body.scope.strip()
        if not fact or not scope:
            raise V2Error(422, "Fact and scope cannot be empty.", code="invalid_preference")
        updated = self.preferences.update(preference_id, fact, scope)
        if updated is None:
            raise _no_preference(preference_id)
        return updated

    def delete_preference(self, preference_id: str, presented: str | None = None) -> None:
        """204: the contract's answer for a deletion, and the honest one — there is no body to
        describe a row that is gone, and `api.ts` returns `undefined` for exactly this status."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if not self.preferences.delete(preference_id):
            raise _no_preference(preference_id)

    # ---- Concept D: the skill library and the learning loop -------------------------------------
    def skills(self) -> list[dict]:
        """The owner's library, as a bare list. `docs/API_V2.md` writes `GET /v2/skills` as
        `SkillSummary[]` and `GET /v2/plans` as `Plan[]`; both are bare here, because a client
        cannot guess which routes wrap."""
        if self.library is None:
            return []
        return [self._skill_summary(row) for row in self.library.summaries()]

    @staticmethod
    def _skill_summary(row: dict) -> dict:
        """The fields `docs/API_V2.md` names for `SkillSummary`, and no more: a response that grows
        a field the UI did not ask for is how a contract becomes a guess.

        `rule` is the ninth, added with Teach Mode and written into the contract in the same edit:
        the owner's own sentence for a skill they taught, and null for the capability the build
        knew how to ask for. Without it a library holding two taught rules is two opaque hashes.
        """
        return {k: row.get(k) for k in ("name", "purpose_plain", "version", "identity", "state",
                                        "learned_at", "last_used_at", "uses", "rule")}

    def teach(self, rule: str, presented: str | None = None) -> dict:
        """`POST /v2/teach` — the owner's sentence becomes a skill, through the same loop.

        The refusals here cost nothing, and that is a property of where they sit rather than a
        hope: `Learner.start_taught` validates the rule BEFORE it starts its thread, so a rule that
        is too long or that asks for an action returns without a model call, without a thread and
        without a library row. `tests/test_teach_e2e.py` pins that by counting the calls the
        provider was actually sent.

        Everything after this point is the Concept D loop unchanged: the same generate, the same
        isolated runner, the same different-family review, the same approval by hash, the same
        library. `GET /v2/learning/{id}`, `/approve`, `/reject` and `/skills/{name}/revoke` are the
        same routes, because it is the same object — a learning, not a second kind of thing.
        """
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.learner is None:
            raise V2Error(503, "learning is not available in this build", code="invalid_stage")
        try:
            learning = self.learner.start_taught(rule)
        except RuleRefused as exc:
            # 422: the request was understood and refused on its content, which is exactly what the
            # code says. Nothing was generated and nothing was spent.
            raise V2Error(422, exc.reason, code=exc.code)
        return {"learning_id": learning.id}

    def learn(self, change_id: str, presented: str | None = None) -> dict:
        """Start learning the capability this change needs.

        When the library ALREADY serves it this is a 409 naming the skill, not a second artifact:
        the owner approved a hash, and quietly minting another one would be a new capability
        wearing the old approval's name. That is a deliberate deviation from a literal reading of
        the contract, which does not say what this case returns.

        A paused routine or a missing connection refuses here too, for the same reason `prepare`
        does — the learning loop reads the same thread and reaches the same providers, and a
        workspace that has been switched off should not start a multi-minute model run."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.learner is None or self.engine is None:
            raise V2Error(503, "learning is not available in this build", code="invalid_stage")
        self._require_ready(change_id)
        if not self.engine.missing_capability():
            row = self.engine.library.active(CAPABILITY)
            raise V2Error(
                409,
                f"the library already serves this change; '{row['name']}' "
                f"({row['identity'][:19]}…) is installed and will be used",
                code="invalid_stage")
        learning = self.learner.start(change_id=change_id)
        # Recorded on the change, so a reload of the change still points at the run the owner
        # started: the panel re-attaches to `change.learning_id`, and an id that only ever lived
        # in the tab that created it is a panel that disappears on refresh.
        change = self.engine.get(change_id)
        if change is not None:
            change.learning_id = learning.id
        return {"learning_id": learning.id}

    def learning(self, learning_id: str) -> dict:
        learning = self.learner.get(learning_id) if self.learner else None
        if learning is None:
            raise _no_learning(learning_id)
        return learning.as_dict()

    def approve_learning(self, learning_id: str, identity: str,
                         presented: str | None = None) -> dict:
        """Install exactly the artifact on the screen. A 409 here means the identity moved — the
        request was fine and the world did not, which is what 409 is for."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.learner is None:
            raise _no_learning(learning_id)
        try:
            return self.learner.approve(learning_id, identity)
        except KeyError as exc:
            raise _no_learning(str(exc))
        except ApprovalNotReady as exc:
            raise V2Error(409, str(exc), code="invalid_stage")
        except ApprovalStale as exc:
            # 409, not 422: nothing about the request was malformed. The hash the owner read is no
            # longer the hash on review, and the only safe answer is "look again".
            raise V2Error(409, str(exc), code="stale_version")

    def reject_learning(self, learning_id: str, presented: str | None = None) -> dict:
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.learner is None:
            raise _no_learning(learning_id)
        try:
            return self.learner.reject(learning_id).as_dict()
        except KeyError as exc:
            raise _no_learning(str(exc))

    def revoke_skill(self, name: str, presented: str | None = None) -> dict:
        """Disable a skill. Future plans cannot use it; an approval already given for a change set
        is a different object and is untouched — revoking a capability is not a way to reach into
        a plan the owner is looking at."""
        self.require_owner(presented, code=OWNER_FORBIDDEN)
        if self.library is None or not self.library.revoke(name):
            raise V2Error(404, f"no active skill '{name}'", code="not_found")
        return {"name": name, "state": "revoked"}

    # ---- the sample's own affordance, outside the contract --------------------------------------
    def next_sample_change(self) -> dict:
        """Hand the sample inbox one more email and answer with the change it becomes.

        This is the sample's demo control and nothing else: `docs/API_V2.md` is explicit that the
        web client's `nextSampleChange()` is a local fixture and NOT an endpoint, and it stays
        that way — this lives at `/sample/next-change`, outside `/v2`, and refuses in live mode.
        It exists because the second half of the product's claim ("the second time, it does not
        ask again") can only be seen on a SECOND change, and a browser cannot reach into a
        Python object to deliver one."""
        if self.mode != "sample":
            raise V2Error(404, "the sample controls are not available in live mode",
                          code="not_found")
        deliver = getattr(self.engine.mail, "deliver", None)
        if deliver is None:
            raise V2Error(409, "this mailbox has no sample deliverable", code="invalid_stage")
        changes = self.engine.changes()
        # The same two guards the web client's own fixture applies, for the same reasons: opening
        # the later request before the first one is finished would hide the thing being shown, and a
        # demo control that delivers a new email every time it is pressed is a demo that runs out
        # of story in front of an audience.
        if not changes or changes[-1].stage != DONE:
            raise V2Error(409, "Finish the first sample change before opening the later request.",
                          code="invalid_stage")
        if self.engine.mail.get_thread(_FOLLOW_UP_THREAD):
            return self.change(changes[-1].id)
        if self.routine.paused:
            raise V2Error(409, "Resume meeting changes before opening the later request.",
                          code="routine_paused")
        before = {c.id for c in changes}
        deliver(_sample_follow_up(), watched=True)
        # The watcher's own tick, and not `engine.detect(None)`. Detection is cursor- and
        # seen-driven; asking for "everything, since the beginning" classified the FIRST email a
        # second time, so one message became two changes and the one the owner was sent to was a
        # change that had never been planned.
        found = [c for c in self.routine.tick() if c.id not in before]
        if not found:
            raise V2Error(409, "nothing new was found in the watched thread",
                          code="invalid_stage")
        return self.change(found[0].id)


#: The thread the demo control's second email arrives on. Named here because three places need to
#: agree on it: the message, the guard that stops a second delivery, and the tests.
_FOLLOW_UP_THREAD = "thread-alex-2"

#: When the second email arrived, and the hour is not decoration — it is the whole reason this is a
#: constant rather than `self._clock()`:
#:
#: * It is the day BEFORE the day being planned, because the request says "tomorrow" and
#:   `_planning_day` reads the day off the message it was handed. Stamped with the service clock it
#:   asked for the 20th: a day this sample has no calendar, no protected block and no research for,
#:   where the second change never reached a proposal at all and the reuse it exists to show could
#:   not be seen.
#: * It is LATER than the first email (09:40Z), because `Routine.tick` reads the inbox through a
#:   cursor that only moves forward. A message stamped behind that watermark is a message that
#:   exists and is never seen — the delivery succeeds and nothing ever becomes a change.
_SAMPLE_FOLLOW_UP_AT = "T11:05:00.000Z"


def _sample_follow_up():
    """The sample's second email: same sender, same request, the day before it plans for.

    Built here rather than in `sim.py` so the simulator stays the description of the DAY and this
    stays the description of one demo step — but fixed, like everything else in the sample, and
    pinned to the simulator's own day rather than to the wall clock, so the two emails cannot
    drift apart."""
    from .ports import MailMessage
    from .sim import SAMPLE_DATE, SAMPLE_OWNER
    day_before = _dt.date.fromisoformat(SAMPLE_DATE) - _dt.timedelta(days=1)
    return MailMessage(
        id="msg-alex-2", thread_id=_FOLLOW_UP_THREAD, sender="Alex Doe <alex@example.com>",
        to=(SAMPLE_OWNER,), subject="Another catch-up to rearrange",
        body_text=("Sorry — one more. Could we use a café near King's Cross tomorrow at 3 or 4? "
                   "Same as before: you choose and update the invite.\n\nAlex"),
        received_at=f"{day_before.isoformat()}{_SAMPLE_FOLLOW_UP_AT}",
        rfc822_message_id="<alex-cafe-2@example.com>")


def _presented(request: Request) -> str | None:
    """The owner token this request carries, if any. `Authorization: Bearer <token>` — the one
    spelling `owner_token.bearer` accepts, and the only place a header is read."""
    return bearer(request.headers.get("authorization"))


#: The `/v2` routes that answer a caller carrying no owner token.
#:
#: This is an ALLOWLIST, and that direction is the whole point. `build_v2_router` gates every route
#: it registers, so a route added tomorrow is the owner's without anyone remembering to say so, and
#: an exemption is a line someone had to write on purpose and defend here.
#:
#: It is keyed by `(METHOD, path)` and not by path, which is the seventh review's second High: a
#: route is exempt only when EVERY method it serves is listed. A `GET` added to this `POST` path is
#: a different route — it reaches a different handler with the same URL — and a path-keyed allowlist
#: would have exempted it without anyone deciding to.
#:
#: There is one, and it earns its place by refusing in live mode before an owner check could even
#: matter: `POST /playground/attack` answers 404 off the sample (`V2Service.attack`), so the only
#: thing it can reach is a sample world built for that request — no owner data, no provider, no
#: network. What it puts at risk is this process, and a rate limit and a CPU cap are what protect
#: that. Everything else on the router is the owner's, including the reads: `/session` alone carries
#: the owner's name, timezone, connection state and protected-window timing, and `/plans` is the
#: index an attacker would page through to reach the change records behind it.
#:
#: This table says which `(METHOD, path)` MAY be exempt. It cannot say WHICH route is, and that is
#: the eighth review's second High: a raw `POST /v2/playground/attack` handler registered straight
#: on the app — ahead of the official route, so it won the match — passed startup validation and
#: returned an owner payload to a caller carrying no token, purely because that method and path are
#: listed here. The exemption is therefore granted by IDENTITY as well as by spelling, at the one
#: moment it is earned: see `_V2_PUBLIC_ROUTES`.
#:
#: The four demo routes earn their place the same way, and are listed here rather than left off
#: because "public by accident" and "public on purpose" look identical from the outside. A demo a
#: stranger cannot use without a credential is not a demo, so the whole point of the mode is an
#: unauthenticated surface — and what that surface can reach is a per-request world over `:memory:`
#: stores with simulated providers, refusing entirely off demo mode (`V2Service._demos` reads the
#: mode and the manager together, so a live build handed one still answers 404). The resource at
#: risk is this process's memory rather than the owner's data, and `DemoSessions` bounds it with a
#: session count, a TTL, a per-client rate limit and a spend ceiling that is checked before any
#: request is sent. `THREAT_MODEL.md` carries the same disclosure for a reader who starts there.
PUBLIC_V2_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/v2/playground/attack"),
    # Start a demo session. Public: a judge with no credential must be able to begin.
    ("POST", "/v2/demo/session"),
    # That session's own state — its budget, its calls left, its seeded inbox.
    ("GET", "/v2/demo/session/{session_id}"),
    # Rewrite one inbound email and run the real pipeline over it. The demo's whole point.
    ("POST", "/v2/demo/session/{session_id}/email"),
    # The same email, through the corpus's own fully-compliant scripted model instead — free, no
    # model call, the engine's other half of the same claim.
    ("POST", "/v2/demo/session/{session_id}/email/compromised"),
})

#: Which `code` a route's owner refusal carries, where `docs/API_V2.md` names one.
#:
#: Defaulting to `owner_forbidden` is what a new route gets and all it can get. These three are the
#: documented exceptions the UI already branches on, and they are preserved rather than collapsed
#: into the default: `approval_forbidden` and `decisions_forbidden` are in the contract, the client
#: reads them, and a fix that quietly retired them would be a fix that changed an API to suit its
#: own plumbing. The route's own `require_owner` call still states each one — this table only tells
#: the ROUTER-level gate which sentence to refuse with, so the outer gate and the inner one agree.
SELF_NAMED_GATE_CODES: dict[str, str] = {
    "/v2/changes/{change_id}/approve": APPROVAL_FORBIDDEN,
    "/v2/changes/{change_id}/decisions": DECISIONS_FORBIDDEN,
    "/v2/changes/{change_id}/model-view": DECISIONS_FORBIDDEN,
}


def _request_argument(handler) -> str:
    """The name of the `request: Request` parameter on a route handler.

    A gate has to read the `Authorization` header, and the only honest way to reach it is a declared
    `Request`. A gate that could not find one would be a gate that lets every caller through, so
    this raises when the router is BUILT rather than when a request arrives: a route that forgets is
    a route that does not start, never a route that quietly serves the public.
    """
    target = inspect.unwrap(handler)
    try:
        hints = typing.get_type_hints(target)
    except Exception:                                            # noqa: BLE001
        # An unresolvable annotation is not a reason to let the route through: fall back to the
        # written annotation, and if that is a string the loop below simply does not match it and
        # the runtime scan in `_owner_gated` is what finds the request.
        hints = {}
    for name, param in inspect.signature(target).parameters.items():
        if hints.get(name, param.annotation) is Request:
            return name
    raise RuntimeError(
        f"the /v2 route {getattr(handler, '__name__', handler)!r} is registered on the "
        f"owner-gated router but declares no `request: Request`, so the gate cannot read the owner "
        f"token and would have to serve every caller. Add the parameter, or name the route in "
        f"`PUBLIC_V2_ROUTES` with the reason it is public.")


def _owner_gated(handler, service):
    """Refuse a caller with no owner token, before the handler runs.

    Applied by `build_v2_router` to every route it registers — nothing here is opt-in, which is the
    property that makes a new route safe by default.

    The refusal is rendered HERE rather than left to `@coded`, because this wrapper sits OUTSIDE it:
    a `V2Error` raised here would reach FastAPI's own handler, which answers `{detail}` alone and
    drops the `code` the browser branches on. Rendering it in the same shape keeps the gate and the
    handlers indistinguishable to every caller, which is what lets the UI treat a refused read the
    same way wherever it is refused.
    """
    argument = _request_argument(handler)
    code = SELF_NAMED_GATE_CODES.get(handler.__dict__.get("__v2_path__", ""), OWNER_FORBIDDEN)

    def presented(*args, **kwargs) -> str | None:
        request = kwargs.get(argument)
        if not isinstance(request, Request):
            request = next((a for a in args if isinstance(a, Request)), None)
        # No request is not "the owner": it is a wiring mistake, and the fail-closed answer is the
        # one that does not depend on the mistake being noticed.
        return _presented(request) if request is not None else None

    def refuse(exc: V2Error) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.detail, "code": exc.code})

    if inspect.iscoroutinefunction(inspect.unwrap(handler)):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            try:
                service.require_owner(presented(*args, **kwargs), code=code)
            except V2Error as exc:
                return refuse(exc)
            return await handler(*args, **kwargs)
        return _stamped(wrapper)

    @functools.wraps(handler)
    def wrapper(*args, **kwargs):
        try:
            service.require_owner(presented(*args, **kwargs), code=code)
        except V2Error as exc:
            return refuse(exc)
        return handler(*args, **kwargs)

    return _stamped(wrapper)


#: Every endpoint function `_owner_gated` has produced. The gate's OWN record that a handler went
#: through it, and the only thing `assert_every_v2_route_is_gated` accepts as proof.
#:
#: The question is asked of the HANDLER rather than of the route's CLASS, and that is the fifth
#: review's High. `_gated_router` sets `route_class=_OwnerGatedRoute`, which is a DEFAULT: FastAPI
#: honours a per-route `route_class_override`, and a route registered that way is built by plain
#: `APIRoute`, skips `_OwnerGatedRoute.__init__` entirely, and never reaches this wrapper. A test
#: that asked "is this route an `_OwnerGatedRoute`" would answer yes for every route that exists and
#: say nothing about the one that does not.
#:
#: Identity in this set, rather than a mark on the handler's `__dict__` (`__v2_owner_gated__`), is
#: the seventh review's Low. A mark is COPYABLE — `functools.wraps` copies `__dict__`, so any future
#: decorator wrapped around a gated handler would have inherited the mark and been reported as gated
#: while the gate itself never ran on the request. A registry cannot be inherited by copying: the
#: copy is a different object, and a different object is not in here.
#:
#: A `WeakSet` rather than a plain set because the routes hold their own endpoints: an entry that is
#: garbage is one no route serves, and `WeakSet.__contains__` answers `False` for an endpoint that
#: cannot be weakly referenced instead of raising — which is the safe direction for a startup check
#: that sweeps every route of an app it did not build.
_V2_GATED_ENDPOINTS: weakref.WeakSet = weakref.WeakSet()


def _stamped(wrapper):
    """Record a wrapper as having gone through `_owner_gated`. See `_V2_GATED_ENDPOINTS`."""
    _V2_GATED_ENDPOINTS.add(wrapper)
    return wrapper


def _is_gated(route) -> bool:
    """Whether this route's handler is behind the owner gate, asked of the gate's own registry."""
    endpoint = getattr(route, "endpoint", None)
    return endpoint is not None and endpoint in _V2_GATED_ENDPOINTS


#: The route objects the gated router ITSELF built at a path `PUBLIC_V2_ROUTES` names.
#:
#: `PUBLIC_V2_ROUTES` is a table of strings, and a string cannot say which route is the vetted one:
#: every route spelled that way inherits the exemption, which is the eighth review's second High.
#: So the exemption is granted by IDENTITY, at the one moment it is earned — `_OwnerGatedRoute`
#: records the object it is building — and `_is_public` asks whether THIS route IS that object. A
#: route registered any other way is a different object and inherits nothing.
#:
#: Not a `set` or a `WeakSet`, which is what `_V2_GATED_ENDPOINTS` gets away with next door: that
#: registry only ever holds FUNCTIONS (`_owner_gated` always returns a `wrapper` function, and a
#: function's `==` is identity), while every route here is a `starlette.routing.Route` subclass —
#: `Route.__eq__` is VALUE-based on `path`, `endpoint` and `methods`, and `Route.__hash__` is `None`,
#: so a set would raise rather than answer and an equality test would call a copied route the vetted
#: one. `is` is the claim, so `is` is the question.
#:
#: Weak references, pruned on the way in: the router already holds its own routes, so a dead
#: reference is a route nothing serves and dropping it keeps a registry written once per router
#: build from growing for the life of the process. A live object's identity cannot be handed to
#: another object, so `is` against a live reference is identity and not a guess.
_V2_PUBLIC_ROUTES: list[weakref.ref] = []


def _remember_public(route) -> None:
    """Record `route` as the vetted route for a `PUBLIC_V2_ROUTES` entry. See `_V2_PUBLIC_ROUTES`."""
    _V2_PUBLIC_ROUTES[:] = [known for known in _V2_PUBLIC_ROUTES if known() is not None]
    _V2_PUBLIC_ROUTES.append(weakref.ref(route))


def _is_public(route) -> bool:
    """Whether this route IS the one the allowlist exempted, rather than merely spelled like it."""
    return any(known() is route for known in _V2_PUBLIC_ROUTES)


def _join(prefix: str, path: str) -> str:
    """One path out of a mount's prefix and the path beneath it, without doubling the separator."""
    if not prefix:
        return path
    if not path:
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _inner_routes(route):
    """The route list this entry delegates to, or `None` when it serves a request itself.

    Both shapes are here for the same reason they were here before: `include_router` does not
    flatten in this FastAPI version — it appends one holder whose routes live on `original_router`
    — while a mount keeps them on `routes`.
    """
    inner = getattr(route, "routes", None)
    if inner is None:
        inner = getattr(getattr(route, "original_router", None), "routes", None)
    return inner


def _walk_routes(routes, prefix: str = ""):
    """Every route reachable from a route list, as `(path, route)` with the path it is SERVED at.

    Descending is what keeps this honest: a walk that only looked at the top level would find
    nothing in this FastAPI version and every assertion over it would pass vacuously.

    The seventh review's High was that it descended into `APIRoute` holders ONLY, so a raw Starlette
    `app.add_route("/v2/internal", ...)` or a `Mount("/v2", subapp)` — neither of which is an
    `APIRoute` — was invisible to the guard and would have answered anonymously. So nothing here
    asks what CLASS a route is: every entry that delegates to a route list is descended into, and
    every entry that does not is yielded, whatever it is. The check is about what the APP serves,
    and the app does not care which class serves it.

    The prefix is what makes a mount's contents findable at all: `Mount("/v2", subapp)` holds routes
    spelled `/internal`, and a caller reaches them at `/v2/internal`. An entry that exposes no route
    list — an opaque ASGI app mounted under `/v2` — is yielded ITSELF rather than skipped, so it
    becomes a route the guard has to decide about instead of a hole in the enumeration.
    """
    for route in routes:
        path = _join(prefix, str(getattr(route, "path", "") or ""))
        inner = _inner_routes(route)
        if inner:
            yield from _walk_routes(inner, prefix=path)
            continue
        yield path, route


def _under_v2(path: str) -> bool:
    """`/v2` and `/v2/...` only.

    `/v2beta` is a different namespace, and a prefix match that did not stop at the separator would
    sweep in every route anyone ever names with those three characters at the front. The resolved
    path is compared rather than the route's own: a route mounted under `/v2` is spelled `/internal`
    on the object and served at `/v2/internal`, and only the second spelling is a namespace.
    """
    return path == "/v2" or path.startswith("/v2/")


def assert_every_v2_route_is_gated(app) -> None:
    """Refuse to serve when any `/v2` route is neither gated nor explicitly public.

    This runs at STARTUP, over the assembled app rather than over the router this module built,
    because the gate's failure mode is a route that never went through this module at all: one
    registered with `route_class_override=APIRoute`, or added straight to the app, or hidden from
    `/openapi.json` with `include_in_schema=False`. Enumerating the app catches all three, and
    enumerating the SCHEMA catches none of them — a hidden route is by definition not in it.

    The default is refusal to start, not a log line. A route that serves owner data anonymously is
    not a degraded mode the process can be useful in, and a warning at startup is a sentence nobody
    reads before the port is open.

    It is a FAIL-FAST and not the boundary, which is the eighth review's first High: the scan is a
    fact about the route table at one moment, and `app.add_api_route("/v2/after-startup", ...)`
    added after this has run answered 200 anonymously because nothing asked again. Every request is
    asked about by `install_v2_request_gate`, which is the boundary; this is the sentence a
    developer gets before the port opens.
    """
    ungated: list[str] = []
    for path, route in _walk_routes(app.routes):
        if not _under_v2(path):
            continue
        # `_is_public` and not a lookup in `PUBLIC_V2_ROUTES`: the exemption belongs to the route the
        # gated router BUILT, not to the spelling of its path, so a route registered another way at
        # an allowlisted `(method, path)` is a route nobody vetted and is caught here. The methods
        # are only read for the message; every method rule is decided when the route is built.
        if _is_gated(route) or _is_public(route):
            continue
        methods = sorted((getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"})
        ungated.append(f"{','.join(methods) or '?'} {path}")
    if ungated:
        raise RuntimeError(
            "these /v2 routes would answer a caller carrying no owner token: "
            + "; ".join(sorted(ungated))
            + ". Register them on the router `build_v2_router` returns, name each of their methods "
              "in `PUBLIC_V2_ROUTES` with the reason it is public, or do not serve them under "
              "`/v2`.")


def _serving_route(routes, scope):
    """The route Starlette is about to dispatch this request to, and how it matched.

    The same loop `Router.app` runs — `route.matches(scope)`, in table order, first `FULL` wins, the
    first `PARTIAL` kept as the fallback the router keeps it as — asked of the same list, so the
    answer cannot disagree with the dispatch that follows it. A second opinion about routing here
    would be a second place for the gate to be wrong, which is why this mirrors the router instead
    of modelling it.

    Descent uses `_inner_routes`, the predicate `_walk_routes` descends by, so the object returned
    is the object the startup guard judged rather than a lookalike beside it. A `FULL` match on an
    entry that delegates to nothing enumerable — an opaque ASGI app mounted under `/v2` — is
    returned as itself, which makes it a route the gate has to decide about instead of a hole.

    `(Match.NONE, None)` when nothing matches: the router answers that with its own 404, and runs no
    handler to answer it with.
    """
    partial = None
    for route in routes:
        match, child_scope = route.matches(scope)
        if match is Match.FULL:
            inner = _inner_routes(route)
            if inner:
                descended = dict(scope)
                descended.update(child_scope)
                return _serving_route(inner, descended)
            return match, route
        if match is Match.PARTIAL and partial is None:
            partial = match, route
    return partial if partial is not None else (Match.NONE, None)


#: What a `/v2` request is told when the route that would answer it cannot prove it is gated.
#:
#: The same shape the route gate refuses in — a `code` the client branches on and a sentence a human
#: can act on — because a caller cannot tell the two refusals apart and should not have to: both
#: mean "this request was not served". The sentence is not the gate's, though, and must not be:
#: presenting the owner token would not open this route, and a refusal that sent the owner looking
#: for a credential they already have would be a worse answer than the truth.
UNGATED_ROUTE_DETAIL = (
    "This route does not go through the owner gate, so it cannot be served. Every /v2 route is "
    "owner-gated by construction; a route that answers without the gate is a defect in the server's "
    "wiring rather than a credential the caller is missing.")


#: What a non-HTTP scope under `/v2` is closed with when its route cannot prove it is gated.
#:
#: `1008` is "policy violation" — the closest thing the WebSocket close codes have to "this server
#: will not serve you", as distinct from a normal closure or a going-away. There is no body to carry
#: `UNGATED_ROUTE_DETAIL` in, so the code is the whole message, and the sentence stays in the log
#: and in this file rather than being invented as a longer frame nobody reads.
#:
#: It is also the code FastAPI closes a WebSocket with when its own dependency solving fails, so the
#: code alone does not say which of the two happened. Said out loud rather than left for a reader to
#: discover: the close is the refusal either way, and a caller that needs to tell them apart is
#: asking a question this boundary does not answer. A code of its own would be a private convention
#: no client knows, which is not more legible, only more distinctive.
UNGATED_ROUTE_CLOSE_CODE = 1008


class _V2RequestGate:
    """Refuse every `/v2` request whose route cannot prove it is gated. Runs on EVERY request.

    The startup scan is a developer's fail-fast — it names the mistake before the port opens. It is
    not the boundary, and the eighth review is why: `app.add_api_route("/v2/after-startup", ...)`
    registered after the startup callback had run answered 200 anonymously, because the scan had
    already happened and nothing asked again. A scan is a fact about the route table at one moment;
    the route table is a list anyone holding the app can still append to.

    So the question is asked again, of the route that is ACTUALLY about to answer: `_serving_route`
    resolves the dispatch the router is about to make, and the request is refused unless that route
    proves gated (through the gate's own registry) or proves exempt (by BEING the vetted route
    object — see `_V2_PUBLIC_ROUTES`). Both proofs are identity, so neither a copied mark nor a
    lookalike route at an allowlisted method and path is accepted.

    What it deliberately does not refuse is a request no handler would serve: a path that matches
    nothing (the router's 404), a method the matched route does not serve (405), or the slash
    redirect. None of those runs a handler, so there is nothing to attribute and nothing to leak,
    and replacing the router's own answer with a less informative one would buy nothing. The
    boundary is about what SERVES, and in every one of those branches nothing does.

    That allowance is for HTTP, and the ninth review is why it stops there. The rule above was
    reached by a branch on `scope["type"] != "http"`, which did not mean "HTTP has this allowance"
    but "everything that is not HTTP is none of my business" — so a WebSocket route registered at
    `/v2/runtime-ws` after startup answered `{"owner_name": "WS_LEAK"}` anonymously, and the gate
    that refuses exactly that route when it is an `APIRoute` waved it through when it was not. The
    scope type is not a reason to stop asking; it is a reason to answer in a different vocabulary.
    So every scope that CARRIES A PATH is judged, and the only scope that is passed through
    unjudged is one that carries none (`lifespan`, which is not a request for a route at all).

    The refusal is written per scope. An HTTP request gets the 403 body above; anything else gets a
    close frame, because there is no response to put a body in. Nothing is passed through on the
    grounds that the gate does not recognise it — "I do not know what this is" is not a reason to
    hand it the owner's data, and an unrecognised path-bearing scope is closed rather than served.
    """

    def __init__(self, app, guarded) -> None:
        self.app = app
        # Read per request rather than captured: the route table is a list that can still be
        # appended to, and seeing it as it is when the request arrives is the entire point.
        self.guarded = guarded

    async def __call__(self, scope, receive, send) -> None:
        # A scope with no `path` is not a request for a route — `lifespan` is the whole of that set
        # in ASGI — so there is nothing here to judge and passing it through takes nothing with it.
        # The test is on the KEY rather than on a list of scope types to accept, so a scope type
        # that does not exist yet is judged rather than grandfathered in by a name nobody updated.
        if "path" not in scope:
            await self.app(scope, receive, send)
            return
        # `get_route_path` and not `scope["path"]`: it is what every route's own `matches` compares
        # against, so a gate that read the raw path would judge a different string than the router
        # routes on the moment a `root_path` is in play.
        if not _under_v2(get_route_path(scope)):
            await self.app(scope, receive, send)
            return
        match, route = _serving_route(self.guarded.routes, scope)
        if match is Match.FULL and (_is_gated(route) or _is_public(route)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            if match is not Match.FULL:
                # A path no route matches, a method the matched route does not serve, the slash
                # redirect: the router's own answer, and no handler runs to give it one. See the
                # class docstring — this allowance is HTTP's, and it is not extended below.
                await self.app(scope, receive, send)
                return
            refused = JSONResponse(status_code=403, content={"detail": UNGATED_ROUTE_DETAIL,
                                                             "code": OWNER_FORBIDDEN})
            await refused(scope, receive, send)
            return
        # Not HTTP, and the route that would serve it has not proved it is gated. There is no
        # response to write a body into, so the refusal is a close frame — sent whether or not a
        # route matched, because a non-HTTP scope under `/v2` that this server does not serve is
        # closed rather than handed to the app to sort out. Deliberately not `await self.app(...)`
        # for an unrecognised scope type: that branch is the ninth review's High, and it is the
        # branch that served a WebSocket route anonymously.
        await send({"type": "websocket.close", "code": UNGATED_ROUTE_CLOSE_CODE})


def _v2_gate_entries(app) -> list:
    """Every entry in `app`'s user middleware stack that IS the per-request gate, in stack order."""
    return [entry for entry in (getattr(app, "user_middleware", None) or [])
            if getattr(entry, "cls", None) is _V2RequestGate]


def _middleware_name(entry) -> str:
    """What to call a middleware entry in a refusal a human has to act on.

    `entry.cls` is the middleware CLASS itself, so its `__name__` is the name to print. Going at it
    through `type(...)` — the obvious-looking spelling — names the METACLASS instead, and every
    middleware in the message comes out as "type", which is a refusal that names nothing and is
    therefore one nobody can act on.
    """
    cls = getattr(entry, "cls", None)
    return getattr(cls, "__name__", None) or type(entry).__name__


def assert_v2_gate_is_innermost(app) -> None:
    """Refuse to serve unless the per-request gate is the INNERMOST user middleware.

    `add_middleware` inserts at the front of `user_middleware`, and `build_middleware_stack` wraps
    that list in reverse — so the front is OUTERMOST and the end is INNERMOST. The gate has to be at
    the end, and the reason is the resolution it makes: `_serving_route` mirrors the router's own
    loop over the same table, so its answer agrees with the dispatch that follows ONLY IF nothing
    runs between the two. A middleware inside the gate breaks exactly that agreement, which is the
    ninth review's third High: a path rewriter installed there let the gate validate `/v2/session`
    and then rewrote the request onto `/v2/inner-leak`, a route nobody vetted, which answered the
    owner's payload as a 200 to a caller carrying no token.

    Outside the gate is not the same risk and is not refused. A middleware out there changes the
    scope BEFORE the gate reads it, so the gate judges the path that will actually be dispatched —
    it can make the gate ask about a different route, but the two still agree, and agreement is the
    whole property. `build_app` relies on this: CORS has to be outermost to put a grant on the
    gate's own refusal, and outermost is a position nothing can be smuggled past.

    This is a separate check from the one in `install_v2_request_gate`, not a duplicate of it. That
    one refuses to CREATE a mis-ordered stack; this one refuses to SERVE one, whatever assembled it
    — including an app whose `user_middleware` was rearranged after the install.
    """
    stack = list(getattr(app, "user_middleware", None) or [])
    if stack and getattr(stack[-1], "cls", None) is _V2RequestGate:
        return
    present = _v2_gate_entries(app)
    if not present:
        raise RuntimeError(
            "this app has no per-request `/v2` gate in its middleware stack, so nothing is asking "
            "each request which route is about to answer it — the ninth review served a route "
            "registered after startup by exactly that omission. Call `install_v2_request_gate(app)` "
            "when the app is built.")
    # `stack.index` is an identity search here, not a value one: `starlette.middleware.Middleware`
    # defines no `__eq__`, so `==` between two entries falls back to `is`, and the entry found is
    # the one `present[-1]` is rather than one built to look like it. Read off that class rather
    # than assumed — a dataclass `Middleware` would compare by value and this line would name the
    # wrong entry, which matters because naming the right one is the whole job of the message.
    inside = ", ".join(sorted({_middleware_name(entry)
                               for entry in stack[stack.index(present[-1]) + 1:]}))
    raise RuntimeError(
        "the per-request `/v2` gate is not the innermost middleware, so the stack between it and "
        f"the route it validated is not empty: {inside}. Whatever is in there can rewrite a request "
        "the gate has already approved onto a route nobody vetted, which is how a gated "
        "`/v2/session` request was served from `/v2/inner-leak`. Install the gate before any other "
        "middleware — `install_v2_request_gate` refuses a stack that is not empty for this reason — "
        "or move that middleware outside the gate.")


def install_v2_request_gate(app) -> None:
    """Make the per-request gate the thing that decides what may be served under `/v2`.

    Called by `build_app` for every app that mounts the v2 router, and by the tests that build an
    app by hand. It has to be installed before the app serves its first request — the middleware
    stack is built once — and it must not be forgotten: an app that mounts the router without this
    is an app whose `/v2` routes are guarded only by the route class, which the eighth review showed
    a route registered another way walks around.

    Installing it is also a claim about ORDER, and this is where the claim is enforced rather than
    assumed. `add_middleware` inserts at the FRONT, and the front is outermost — so every middleware
    already in the stack ends up INSIDE the gate, between it and the dispatch, which is the ninth
    review's third High. A stack that is not empty at install time is therefore refused instead of
    being silently mis-ordered (`assert_v2_gate_is_innermost` states the invariant and re-checks it
    on the assembled app). Middleware added AFTERWARDS is fine and is not refused: it lands at the
    front, outside, where it cannot get between the gate and the route it approved.
    """
    if app.user_middleware:
        already = ", ".join(sorted({_middleware_name(entry) for entry in app.user_middleware}))
        raise RuntimeError(
            "`install_v2_request_gate` was called on an app that already has middleware "
            f"({already}). `add_middleware` inserts at the front, so those would be installed "
            "INSIDE the gate — able to rewrite a request the gate has already approved onto a route "
            "nobody vetted, which is how a gated `/v2/session` request was served from "
            "`/v2/inner-leak`. Install the gate first and add the rest afterwards; they will land "
            "outside it, where they cannot.")
    app.add_middleware(_V2RequestGate, guarded=app)


def _gated_router(service, *, prefix: str, tags: list[str]) -> APIRouter:
    """An `APIRouter` whose every route is owner-gated unless `PUBLIC_V2_ROUTES` says otherwise.

    The gate is applied by the ROUTE CLASS rather than by a decorator on each handler. A decorator
    is a line someone can forget, and the defect being fixed is exactly that five routes were
    written without one; a route class cannot be forgotten, because a route that is not registered
    through it is not a route.
    """

    class _OwnerGatedRoute(APIRoute):
        def __init__(self, path, endpoint, **kwargs):
            # `path` is already `prefix + path` — see `APIRouter.add_api_route`. It is the same
            # spelling `PUBLIC_V2_ROUTES` and `SELF_NAMED_GATE_CODES` are keyed by, so the three
            # tables describe one namespace rather than three.
            methods = [str(m).upper() for m in (kwargs.get("methods") or ["GET"])]
            # Every method this route serves has to be exempt for the route to be exempt: the
            # allowlist is keyed by `(method, path)`, and exempting a whole route because one of its
            # methods is listed is how `GET` on a listed `POST` path would answer anonymously.
            if not all((method, path) in PUBLIC_V2_ROUTES for method in methods):
                # The handler is stamped before it is wrapped so that the gate can find the code
                # the route's own `require_owner` call uses. `functools.wraps` copies `__dict__`,
                # so a stamp on the innermost function survives the `@coded` wrapper. Assigned
                # rather than defaulted: the path is a fact about THIS registration, and a value
                # inherited from somewhere else would name the wrong route.
                endpoint.__dict__["__v2_path__"] = path
                endpoint = _owner_gated(endpoint, service)
                super().__init__(path, endpoint, **kwargs)
                return
            # The exemption is granted to THIS object, here, once — see `_V2_PUBLIC_ROUTES`. Any
            # other way of reaching an allowlisted `(method, path)` builds a different object and
            # inherits nothing, which is what makes the exemption a fact about a route rather than
            # about a spelling. Recorded after `super().__init__` so that a route the framework
            # refused to build is never recorded as one that may serve.
            super().__init__(path, endpoint, **kwargs)
            _remember_public(self)

    return APIRouter(prefix=prefix, tags=tags, route_class=_OwnerGatedRoute)


def build_v2_router(service: V2Service, *, prefix: str = "/v2",
                    trust_proxy: bool | None = None,
                    trusted_hops: int | None = None) -> APIRouter:
    # Every route below is owner-gated unless `PUBLIC_V2_ROUTES` names it. The gate reads the token
    # off the request and refuses before the handler runs; in sample mode `is_owner` answers from the
    # sample's one explicit claim, so this costs the demo nothing.
    router = _gated_router(service, prefix=prefix, tags=["v2"])
    #: A second name for the same object, because the path template `/connections/{service}` needs
    #: a parameter called `service` and a handler that shadows the service it is calling is a
    #: handler that calls a string.
    _service = service

    @router.get("/session")
    @coded
    def session(request: Request) -> dict:
        return service.session(_presented(request))

    @router.post("/routine")
    @coded
    def routine(body: RoutineBody, request: Request) -> dict:
        return service.set_routine(body.paused, _presented(request))

    @router.post("/boundary")
    @coded
    def boundary(body: BoundaryBody, request: Request) -> dict:
        return service.set_boundary(body.start, body.buffer_minutes, _presented(request))

    @router.get("/limits")
    @coded
    def limits(request: Request) -> dict:
        return service.limits_state(_presented(request))

    @router.post("/limits")
    @coded
    def set_limits(body: LimitsBody, request: Request) -> dict:
        return service.set_limits(body.mode, _presented(request))

    @router.post("/connections/{service}")
    @coded
    def connect(service: str, request: Request) -> dict:
        # The parameter is `service` because that is the path template the contract names; the
        # closure's service object is reached as `service` too, which is exactly why this calls
        # through a local alias rather than shadowing the name it needs.
        #
        # The request is declared so the owner gate can refuse an anonymous caller: attaching a
        # service used to mark the row `connected` for whoever asked, which is an authorization
        # state a stranger could set. The token is carried through to the session body as well, so
        # the screen that just changed something as the owner is not told it is not the owner.
        return _service.connect(service, _presented(request))

    @router.get("/today")
    @coded
    def today(request: Request, date: str | None = None) -> dict:
        # `request` is unused by the body and is here for the gate above it, which is the only
        # thing that reads the owner token. It is a parameter rather than something the gate digs
        # out for itself because FastAPI injects it by signature, and a gate that guessed would be
        # a gate that guessed wrong once and served the public.
        return service.today(date)

    @router.post("/changes/{change_id}/prepare")
    @coded
    def prepare(change_id: str, request: Request) -> dict:
        return service.prepare(change_id, _presented(request))

    @router.get("/changes/{change_id}")
    @coded
    def change(change_id: str, request: Request) -> dict:
        return service.change(change_id)

    @router.post("/changes/{change_id}/leave")
    @coded
    def leave(change_id: str, request: Request) -> dict:
        return service.leave(change_id, _presented(request))

    @router.post("/changes/{change_id}/edit")
    @coded
    def edit(change_id: str, body: EditBody, request: Request) -> dict:
        return service.edit(change_id, body, _presented(request))

    @router.post("/changes/{change_id}/approve")
    @coded
    def approve(change_id: str, body: ApproveBody, request: Request) -> dict:
        return service.approve(change_id, body.version, _presented(request))

    @router.get("/changes/{change_id}/decisions")
    @coded
    def decisions(change_id: str, request: Request) -> list:
        return service.decisions(change_id, _presented(request))

    @router.get("/changes/{change_id}/model-view")
    @coded
    def model_view(change_id: str, request: Request) -> dict:
        return service.model_view(change_id, _presented(request))

    #: Resolved once here, not per request. See `_trust_proxy` and `_trusted_proxy_hops`.
    _trust = _trust_proxy(trust_proxy)
    _hops = _trusted_proxy_hops(trusted_hops)

    @router.post("/playground/attack")
    @coded
    async def attack(request: Request) -> dict:
        # The body is read here rather than declared as a parameter, and that is the finding: a
        # declared model is parsed by FastAPI from a body it has already read and JSON-decoded in
        # full, so a `max_length` on a field is a limit that fires after the memory is spent. This
        # is the only route whose body is attacker-controlled bytes rather than a client's own
        # JSON, so it is the only one that reads its own.
        return service.attack(_attack_email(await _read_capped(request)),
                              _presented(request), _client(request, _trust, _hops))

    # ---- the public demo -------------------------------------------------------------------------
    #
    # Four routes, all named in `PUBLIC_V2_ROUTES` with their reason, and all refusing with a 404
    # off demo mode (see `V2Service._demos`). They are public because the point of the mode is that
    # a stranger with no credential can use it — that is what "a working demo" means — and the
    # thing a caller reaches is a world built for their request: `:memory:` stores, simulated
    # providers, a session token the server holds. The cap that stands in for the owner gate is
    # the budget, and it is enforced before any request is sent rather than after it is billed.
    @router.post("/demo/session")
    @coded
    def demo_session_start(request: Request) -> dict:
        return service.demo_start(_client(request, _trust, _hops))

    @router.get("/demo/session/{session_id}")
    @coded
    def demo_session_state(session_id: str, request: Request) -> dict:
        return service.demo_state(session_id)

    @router.post("/demo/session/{session_id}/email")
    @coded
    async def demo_session_email(session_id: str, request: Request) -> dict:
        # Read like the playground's, and for the same reason: this is the other route whose body
        # is a stranger's bytes, so a declared pydantic model would be a limit that fires after
        # the memory is already spent.
        return service.demo_email(session_id, **_demo_email_body(await _read_capped(request)))

    @router.post("/demo/session/{session_id}/email/compromised")
    @coded
    async def demo_session_email_compromised(session_id: str, request: Request) -> dict:
        # Same read, same reason as `demo_session_email` above: a stranger's bytes, capped before
        # they are parsed.
        return service.demo_email_compromised(
            session_id, **_demo_email_body(await _read_capped(request)))

    @router.get("/changes/{change_id}/outcome")
    @coded
    def outcome(change_id: str, request: Request) -> dict:
        return service.outcome(change_id)

    @router.get("/changes/{change_id}/why")
    @coded
    def why(change_id: str, request: Request) -> dict:
        return service.why(change_id)

    @router.get("/plans")
    @coded
    def plans(request: Request) -> list[dict]:
        # The index an attacker would page through: every plan names a change id, and every change
        # id opens a record with the owner's meeting, its excerpt and its recipients on it.
        return service.plans()

    @router.get("/preferences")
    @coded
    def preferences(request: Request) -> list[dict]:
        return service.list_preferences()

    @router.post("/preferences")
    @coded
    def add_preference(body: PreferenceBody, request: Request) -> dict:
        return service.add_preference(body, _presented(request))

    @router.patch("/preferences/{preference_id}")
    @coded
    def update_preference(preference_id: str, body: PreferencePatch, request: Request) -> dict:
        return service.update_preference(preference_id, body, _presented(request))

    @router.delete("/preferences/{preference_id}", status_code=204)
    @coded
    def delete_preference(preference_id: str, request: Request) -> None:
        return service.delete_preference(preference_id, _presented(request))

    # ---- agreements ---------------------------------------------------------------------------
    @router.get("/agreements")
    @coded
    def agreements(request: Request) -> dict:
        return service.agreements(_presented(request))

    @router.post("/agreements/check")
    @coded
    def check_agreements(request: Request) -> dict:
        return service.check_agreements(_presented(request))

    # ---- the care circle ----------------------------------------------------------------------
    # Owner-token-gated on every verb, including GET. See `care_circle_state` for why a read of
    # this is the owner's: the answer names the people in their parent's life.
    @router.get("/care-circle")
    @coded
    def care_circle(request: Request) -> dict:
        return service.care_circle_state(_presented(request))

    @router.post("/care-circle", status_code=201)
    @coded
    def add_care_member(body: CareMemberBody, request: Request) -> dict:
        return service.add_care_member(body.name, body.email, body.may_receive,
                                       _presented(request))

    @router.delete("/care-circle/{member_id}", status_code=204)
    @coded
    def remove_care_member(member_id: str, request: Request) -> None:
        return service.remove_care_member(member_id, _presented(request))

    # ---- the digest ---------------------------------------------------------------------------
    # The one route that SENDS something to the circle, and therefore the one that has to be the
    # owner's. `?day=` is optional and is the only input: the audience is read from the circle.
    @router.post("/care/digest")
    @coded
    def send_digest(request: Request, day: str | None = None) -> dict:
        return service.send_digest(day, _presented(request))

    # ---- protected facts ----------------------------------------------------------------------
    # No verb here returns a value, and that is the contract rather than an omission: a route that
    # could hand one back would be the disclosure path the whole feature exists to close.
    @router.get("/protected-facts")
    @coded
    def protected_facts(request: Request) -> list[dict]:
        return service.protected_facts_state(_presented(request))

    @router.post("/protected-facts", status_code=201)
    @coded
    def add_protected_fact(body: ProtectedFactBody, request: Request) -> dict:
        return service.add_protected_fact(body.label, body.kind, body.value,
                                          _presented(request))

    @router.delete("/protected-facts/{fact_id}", status_code=204)
    @coded
    def remove_protected_fact(fact_id: str, request: Request) -> None:
        return service.remove_protected_fact(fact_id, _presented(request))

    # ---- Concept D ----------------------------------------------------------------------------
    @router.get("/skills")
    @coded
    def skills(request: Request) -> list[dict]:
        return service.skills()

    @router.post("/changes/{change_id}/learn")
    @coded
    def learn(change_id: str, request: Request) -> dict:
        return service.learn(change_id, _presented(request))

    @router.post("/teach")
    @coded
    def teach(body: TeachBody, request: Request) -> dict:
        return service.teach(body.rule, _presented(request))

    @router.get("/learning/{learning_id}")
    @coded
    def learning(learning_id: str, request: Request) -> dict:
        return service.learning(learning_id)

    @router.post("/learning/{learning_id}/approve")
    @coded
    def approve_learning(learning_id: str, body: IdentityBody, request: Request) -> dict:
        return service.approve_learning(learning_id, body.identity, _presented(request))

    @router.post("/learning/{learning_id}/reject")
    @coded
    def reject_learning(learning_id: str, request: Request) -> dict:
        return service.reject_learning(learning_id, _presented(request))

    @router.post("/skills/{name}/revoke")
    @coded
    def revoke_skill(name: str, request: Request) -> dict:
        return service.revoke_skill(name, _presented(request))

    return router


def build_sample_router(service: V2Service, *, prefix: str = "/sample") -> APIRouter:
    """The demo controls, deliberately outside `/v2`.

    `/v2` is the contract `web/lib/api.ts` is written against. A route that exists only because
    the sample has a scripted inbox would be a route every live caller has to be told to ignore,
    so it lives under its own prefix and refuses unless the service is in sample mode."""
    router = APIRouter(prefix=prefix, tags=["sample"])

    @router.post("/next-change")
    @coded
    def next_change() -> dict:
        return service.next_sample_change()

    return router


#: Stages where the change is finished with the owner. An open change is one that still needs a
#: decision, and "this needs you" is not a fact about a timestamp.
_SETTLED = ("stopped", "done", "partial")


def _belongs_to_day(change, day: str) -> bool:
    """Is this change part of that day?

    A change arrives one day and is about another. Alex's email lands on the 18th asking to move the
    19th's meeting: the 18th is when it needs the owner, the 19th is what it is about. Answering
    only the first is why an owner who opens the workspace on the day they are planning saw an empty
    screen — the day's events were there and the thing that needed deciding was not.

    So a change is on a day when it arrived that day, when the plan is for that day, or when it
    arrived earlier and is still open.
    """
    received = str(change.received_at)[:10]
    return (received == day or change.plan_day == day
            or (change.stage not in _SETTLED and received <= day))


def _missing(change_id: str) -> V2Error:
    return V2Error(404, f"no change '{change_id}'", code="not_found")


def _no_learning(learning_id: str) -> V2Error:
    return V2Error(404, f"no learning '{learning_id}'", code="not_found")


def _no_preference(preference_id: str) -> V2Error:
    return V2Error(404, f"no preference '{preference_id}'", code="not_found")


def _stale(exc: Exception) -> V2Error:
    """The world moved under a reviewed plan. 409 with `stale_version`, which is the code the web
    client uses to tell "review it again" apart from "the request was wrong"."""
    return V2Error(409, str(exc), code="stale_version")


def _reconnect(exc: ReauthRequired) -> V2Error:
    """The provider is not connected, and nothing was sent. The detail is the connector's own
    message, which already names the one command that fixes it."""
    return V2Error(503, str(exc), code="connection_required")


def _refused(exc: EgressRefused) -> V2Error:
    """A refusal the UI can render. The exception's own text names the field and the rule; it
    never contains the protected value, because the gate does not put it there."""
    return V2Error(422, str(exc), code="boundary_conflict")


def _thread_url(change) -> str | None:
    """The link to the original thread, or None.

    None is a real answer — `docs/API_V2.md` says `ChangeSummary.thread_url` may be null — and it
    is the honest one for a message that never came from Gmail. The UI already renders that case
    ("original thread is synthetic"); a fabricated Gmail URL is a link that 404s for the owner and
    looks like a bug in the product rather than a property of the sample."""
    if getattr(change, "thread_id", "") and str(change.message_id).startswith(("m", "msg-")):
        return f"https://mail.google.com/mail/u/0/#all/{change.thread_id}"
    return None


def _moment(stamp: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(str(stamp))


def _day(date: str | None, moment: _dt.datetime) -> tuple[str, str]:
    """The requested date and the owner's UTC offset at it, so the day boundary is the OWNER's
    midnight rather than the server's. No date means today, read from the same clock the validator
    uses — a second `now()` here is how "today" and "the day being planned" drift apart."""
    offset = moment.strftime("%z")
    return (date or moment.date().isoformat()), f"{offset[:3]}:{offset[3:]}"
