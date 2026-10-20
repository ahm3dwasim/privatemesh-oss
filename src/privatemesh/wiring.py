"""The composition root: one v2 product, two ways of attaching it to the world.

The engine (`changes`), the review bundle (`changeset`), the effects (`execute`), the private
boundary (`boundary`), the watcher (`routine`) and the HTTP surface (`api_v2`) were built against
`ports.py` and had never run together. This module is the single place that puts them together,
and the only place that decides what "sample" and "live" MEAN — the engine is handed ports and
cannot tell which it got, which is what makes the sample a preview of the real product rather
than a mock-up of one.

Two rules this module holds to:

* **A mode is a claim about the providers, not a flag in the code.** `mode="sample"` means every
  provider is a deterministic simulator and `GET /v2/session` says so on every row.
  `mode="live"` means the collaborators are the real Gmail/Calendar/Tavily connectors and the
  real Token Factory behind the spend ledger and the privacy gate. Nothing downstream branches on
  the mode; the ports are simply different objects.
* **A missing credential is a STATE, not a crash.** `build_live` never raises because a token is
  absent or expired: it reports per-connection state, so the UI can say "Reconnect Google" and
  the process stays up. `require_ready` is the separate, explicit refusal the CLI calls at
  startup — refusing to serve a live product that cannot reach a provider is the fail-closed
  choice, and it is a decision made once, out loud, rather than a 500 discovered later.

The sample's scripted model routes on the system prompt the engine actually sent and on the
payload it actually built (`sample_reply`), never on a caller-set flag: change one of the engine's
prompts and the sample stops answering, loudly, instead of quietly following along.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .api_v2 import V2Service
from .approval import ApprovalStore
from .audit import AuditLog
from .boundary import BoundaryStore, GuardedSearch, PrivacyGate
from .care import CareCircleStore, DigestLedger, ProtectedFactStore
from .broker import Broker
from .changes import (CHOOSE_SYSTEM, CLASSIFY_SYSTEM, DRAFT_SYSTEM, EXTRACT_SYSTEM, QUERY_SYSTEM,
                      ChangeEngine, ChangeStore)
from .changeset import ChangeSetStore
from .config import Settings
from .demo_session import (DEMO_MODE, DemoRefused, real_provider_credentials,
                           refuse_if_multi_worker, refuse_if_providers_configured)
from .execute import ApprovedBundles, Executor, build_effect_skills
from .gcal import GCalClient
from .gmail import DEFAULT_LABEL, GmailClient
from .google_auth import GoogleAuth
from .learn import CAPABILITY, GENERATE_SYSTEM, REVIEW_SYSTEM, TEACH_GENERATE_SYSTEM, Learner
from .library import SkillLibrary
from .llm import TokenFactory
from .limits import LimitStore
from .ports import ProtectedCommitment, ReauthRequired
from .routine import DEFAULT_INTERVAL_SECONDS, Routine
from .sample_skill_code import generation_envelope, review_answer, taught_generation_envelope
from .agreements import AgreementChecker, AgreementStore
from .sim import (SAMPLE_DATE, SamplePorts, build_care_week_ports, build_sample_ports,
                  sample_agreements)
from .skills import SkillRegistry
from .spend import SpendLedger
from .tavily_evidence import TavilySearch

log = logging.getLogger(__name__)

SAMPLE_MODE, LIVE_MODE = "sample", "live"
#: `DEMO_MODE` is imported rather than spelled again: the name is part of the HTTP contract (it is
#: what `GET /v2/session` reports and what the route gate branches on), so two spellings of it in
#: two modules would be two things to keep in step. Its meaning, its caps and its session store all
#: live in `demo_session`, which is where the mode actually is.
SAMPLE_OWNER_NAME = "Sam Owner"
SAMPLE_TIMEZONE = "Europe/London"

#: The models a demo run uses when `PM_MODEL_*` is unset. Measured on Token Factory 2026-09-18 and
#: quoted in `.env.example`, so a demo started with nothing but `NEBIUS_API_KEY` still calls real
#: Nemotron rather than refusing over a variable the operator was never told to set.
DEMO_MODEL_DEFAULTS = {
    "model_fast": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
    "model_reason": "nvidia/nemotron-3-super-120b-a12b",
    "model_deep": "nvidia/Nemotron-3-Ultra-550b-a55b",
}

#: The sample's "now": the morning of the day the plan is for, which is the day the web client's
#: Today screen shows. Alex's email arrived the day before (it says "tomorrow"), so the change is
#: still open and still needs the owner. Fixed, like everything else in the sample — a demo that
#: drifts with the wall clock is a demo that fails — and pinned to the day rather than to the
#: morning the email landed, because `/v2/today` with no date answers about the clock's day, and a
#: workspace that opens on an empty yesterday does not tell the story the client tells.
SAMPLE_NOW = "2026-09-19T09:00:00+01:00"

#: Words that make an email a meeting-change request. The scripted classifier is a keyword rule
#: ON PURPOSE: the product's claim is about the boundary and the approval, and a sample that
#: needed a live model to classify would not be a sample a judge can run.
MEETING_CHANGE_RE = re.compile(r"\b(meet|meeting|invite|reschedul\w*|calendar|move)\b", re.I)
#: Words that make an email a refill confirmation — the same keyword-rule idiom as
#: `MEETING_CHANGE_RE`, and checked only when that one already said no: the two questions read as
#: mutually exclusive in `changes.classify`, and the scripted model answers them that way too.
#: `sim.caregiver_messages`'s `msg-refill` ("Your repeat prescription is ready" / "...ready to
#: collect...") is the worked example this rule is built to read.
_REFILL_RE = re.compile(r"\b(prescription|refill)\b", re.I)
_COLLECT_RE = re.compile(r"\bcollect", re.I)
_CLOSED_RE = re.compile(r"\bclosed\b", re.I)
#: Words that end a place name in "near X tomorrow at 3": the place is what is left.
_PLACE_STOP_RE = re.compile(
    r"\b(tomorrow|today|tonight|at|on|this|next|morning|afternoon|evening)\b", re.I)
_PLACE_RE = re.compile(r"\b(?:near|in|around|at)\s+(.{3,60})", re.I)


class ProviderUnavailable(RuntimeError):
    """A live collaborator could not be built (no key), so nothing can run. Nothing was sent."""


class UnavailableModel:
    """Stands in for `TokenFactory` when there is no Nebius key.

    It exists so the app comes up and REPORTS the state instead of failing to import, and it
    raises on use: a model that quietly answers nothing would turn "not configured" into "the plan
    came back empty", which is a different and much worse sentence."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def chat(self, *args, **kw) -> dict:
        raise ProviderUnavailable(self.reason)


# ---------------------------------------------------------------- the scripted model -----------
def _payload(user: str) -> dict:
    """The JSON object the engine put in the user message. Empty when it is not one."""
    try:
        value = json.loads(user or "")
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _offset_for(timezone: str, day: _dt.date, *, fallback: str = "+00:00") -> str:
    """The owner's UTC offset on that day. Asked of the timezone database, never of the server's
    own clock: a 15:00 meeting is a claim about a specific hour in a specific place."""
    try:
        moment = _dt.datetime(day.year, day.month, day.day, 12, tzinfo=zoneinfo.ZoneInfo(timezone))
        return moment.strftime("%z")[:3] + ":" + moment.strftime("%z")[3:]
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, TypeError):
        return fallback


def _planning_day(payload: dict) -> tuple[str, str] | None:
    """The day the request is about: the day AFTER the newest message in the thread.

    The email says "tomorrow at 3 or 4", so the day to plan is tomorrow — computed from the
    message the engine actually handed over, not from a constant. The offset comes from the
    owner's timezone (which the engine sends), with the allowed planning fact as the fallback,
    because a time without the right offset is a different hour."""
    stamps = [m.get("received_at") for m in payload.get("thread") or [] if isinstance(m, dict)]
    moments = []
    for stamp in stamps:
        try:
            moments.append(_dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            continue
    if not moments:
        return None
    day = (max(moments) + _dt.timedelta(days=1)).date()
    fallback = "+00:00"
    fact_start = (payload.get("protected") or {}).get("start")
    if isinstance(fact_start, str) and len(fact_start) >= 6 and fact_start[-6] in "+-":
        fallback = fact_start[-6:]
    return day.isoformat(), _offset_for(str(payload.get("timezone") or ""), day, fallback=fallback)


def _excerpt(text: str, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def _place(request: str) -> str:
    """ "Could we meet somewhere near King's Cross tomorrow at 3 or 4?" -> "King's Cross"."""
    match = _PLACE_RE.search(request or "")
    if not match:
        return ""
    return " ".join(_PLACE_STOP_RE.split(match.group(1))[0].split()).strip(" .,;:—-")


def _classify(payload: dict) -> dict:
    text = f"{payload.get('subject') or ''} {payload.get('body') or ''}"
    is_change = bool(MEETING_CHANGE_RE.search(text))
    is_refill = (not is_change and bool(_REFILL_RE.search(text))
                and bool(_COLLECT_RE.search(text)))
    return {"is_meeting_change": is_change, "is_refill_confirmation": is_refill,
            "reason": ("the sender proposes a different time or place" if is_change
                       else "a ready prescription asks to be collected" if is_refill
                       else "nothing in it asks to change a meeting")}


def _extract(payload: dict) -> dict:
    request = _excerpt(payload.get("request") or "", 140)
    if not request:
        newest = max((m for m in payload.get("thread") or [] if isinstance(m, dict)),
                     key=lambda m: str(m.get("received_at") or ""), default={})
        request = _excerpt(newest.get("body") or "", 140)
    answer: dict = {"request_excerpt": request}
    day = _planning_day(payload)
    if day is None:
        # No readable date on the thread: offer nothing rather than a guess, which the engine
        # turns into an honest "no safe fit" instead of a meeting on the wrong day.
        return {**answer, "options": []}
    date, offset = day
    # The two times the email actually proposes, as instants: the engine's validator — not the
    # model — decides which of them survives the protected window.
    answer["options"] = [
        {"start": f"{date}T16:00:00{offset}", "end": f"{date}T16:45:00{offset}", "phrase": "4"},
        {"start": f"{date}T15:00:00{offset}", "end": f"{date}T15:45:00{offset}", "phrase": "3"},
    ]
    return answer


def _query(payload: dict) -> dict:
    if payload.get("already_seen"):
        # The results are in and the question is answered: the scripted model stops rather than
        # spending the second search round on a query it has already run.
        return {"query": "", "include_domains": []}
    place = _place(str(payload.get("request") or ""))
    if not place:
        return {"query": "", "include_domains": []}
    return {"query": f"{place} café opening hours", "include_domains": []}


def _choose(payload: dict) -> dict:
    """Pick one result and reject one, from the evidence as it came back.

    The rejection is not decoration: the direction asks for one refused option WITH its reason,
    so the scripted model looks for a source that says "closed" and names it. Nothing is invented
    — if no result can be stood behind, the proposal carries that uncertainty instead."""
    results = [r for r in payload.get("results") or [] if isinstance(r, dict)]
    closed = [i for i, r in enumerate(results) if _CLOSED_RE.search(str(r.get("excerpt") or ""))]
    chosen = next((i for i, r in enumerate(results) if r.get("official") and i not in closed), None)
    if chosen is None:
        chosen = next((i for i, r in enumerate(results) if i not in closed), None)
    rejected = [{"index": i, "reason": f"its own page says it is closed then: "
                                       f"{_excerpt(results[i].get('excerpt'), 160)}"}
                for i in closed[:1]]
    if chosen is None:
        return {"venue_index": None, "name": "Venue to confirm", "address": "",
                "reason_sentence": "No result could be supported for that time.",
                "is_venue": False, "rejected": rejected}
    return {"venue_index": chosen, "name": str(results[chosen].get("title") or ""), "address": "",
            "reason_sentence": ("Its own published hours cover the time you asked about"
                                + (", and the alternative is closed then." if rejected else ".")),
            # The scripted model is held to the same question the real one is asked: the sample's
            # results are café pages, so it answers true — and a sample whose results were station
            # pages would have to answer false, exactly as the live model should have.
            "is_venue": True, "rejected": rejected}


def _draft(payload: dict) -> dict:
    """The reply, CHOSEN from the pieces the engine rendered, never written.

    The fourth review's structural finding landed here as well as in the gate. This used to write
    its own sentences and label them with an allowlisted source, which is the arrangement the whole
    matcher problem came out of: the source was a claim and the text was the model's, so the only
    thing standing between a sentence and the wire was a scan. The engine now hands the draft a
    catalogue in `pieces` — every sentence this reply may contain, rendered from the plan's own
    facts — and the scripted model picks from it, exactly as the live one is asked to.

    It picks by INDEX into what it was given rather than by writing the words down, so there is no
    string here that could disagree with the catalogue, and the sample cannot demonstrate an email
    the real gate would have refused. What it chooses is deliberately the same reply it used to
    write: the greeting, the proposed slot, and the venue."""
    pieces = list(payload.get("pieces") or ())
    by_source = {}
    for index, piece in enumerate(pieces):
        by_source.setdefault(str(piece.get("source") or ""), index)
    chosen = [index for index in (by_source.get("template"), by_source.get("recipient"),
                                  by_source.get("time"), by_source.get("venue"))
              if index is not None]
    return {"template_body": str(pieces[by_source["template"]].get("text") or "")
                            if "template" in by_source else "",
            "fragments": [{"source": str(pieces[i].get("source") or ""),
                           "text": str(pieces[i].get("text") or "")} for i in chosen
                          if str(pieces[i].get("source") or "") != "template"]}


_ANSWERS: tuple[tuple[str, Callable[[dict], dict]], ...] = (
    (CLASSIFY_SYSTEM, _classify),
    (EXTRACT_SYSTEM, _extract),
    (QUERY_SYSTEM, _query),
    (CHOOSE_SYSTEM, _choose),
    (DRAFT_SYSTEM, _draft),
    # The two learning-loop prompts, answered from the canned generator/reviewer. Routed on the
    # prompt's own text like every other entry here: `learn.py` has no sample branch, so the
    # scripted answers arrive through the same door as a real model's would.
    (GENERATE_SYSTEM, generation_envelope),
    (REVIEW_SYSTEM, review_answer),
    # Teach Mode's generator. Its own marker, because the contract it answers is a different one —
    # the owner's rule instead of the build's capability, and the labelled examples on top.
    (TEACH_GENERATE_SYSTEM, taught_generation_envelope),
)


def sample_reply(body: dict) -> str:
    """The scripted model's answer to ONE Token Factory request body — pure and deterministic.

    Routing is on the engine's own system prompts (the constants in `changes`), so the sample
    answers the REAL prompts rather than a test double's idea of them. An unrecognised prompt gets
    an empty object, which the engine's single repair attempt turns into a visible failure instead
    of a plausible-looking wrong answer."""
    messages = body.get("messages") or []
    system = next((str(m.get("content") or "") for m in messages
                   if m.get("role") == "system"), "")
    user = next((str(m.get("content") or "") for m in reversed(messages)
                 if m.get("role") == "user"), "")
    for marker, answer in _ANSWERS:
        if system.startswith(marker):
            return json.dumps(answer(_payload(user)), ensure_ascii=False)
    return "{}"


#: The sample's settings, written out. `Settings.load()` reads the process environment and, before
#: that, reads a `.env` out of the working directory — which is a thing a STARTUP does, once, for a
#: build whose whole purpose is to be configured by whoever is deploying it. It is not a thing a
#: REQUEST does. The playground is a public, unauthenticated route, and every attack it runs
#: builds a sample model; when that model reached for `sample_settings(None)` it reached through
#: to `Settings.load()`, so a stranger could make the server read `.env` and copy its contents into
#: `os.environ` once per request. The literals below are the whole of what the sample needs, and
#: nothing on a request path consults the environment to find them.
SAMPLE_SETTINGS = Settings(api_key="sample-no-key",
                           base_url="https://api.tokenfactory.nebius.com/v1/",
                           model_fast="nemotron-nano-sample",
                           model_reason="nemotron-super-sample",
                           model_deep="nemotron-ultra-sample", tavily_key="")


def sample_settings(base: Settings | None = None) -> Settings:
    """The sample's settings: a placeholder key and named sample models, so a sample run can never
    reach a provider or spend anything even if a real `.env` is present.

    The fallback is `SAMPLE_SETTINGS` and never `Settings.load()`. A caller that has settings of
    its own still passes them and still gets them back with the sample's key and model names
    substituted — what changed is only the case where the caller had none, which used to read the
    environment and now does not."""
    base = base or SAMPLE_SETTINGS
    return dataclasses.replace(base, api_key="sample-no-key", model_fast="nemotron-nano-sample",
                               model_reason="nemotron-super-sample",
                               model_deep="nemotron-ultra-sample", tavily_key="")


def sample_model(settings: Settings | None = None, *, audit=None, gate=None,
                 client: httpx.Client | None = None) -> TokenFactory:
    """The real `TokenFactory` over a scripted transport — same client, same tier router, same
    token accounting, so `/usage` reports genuine bookkeeping over canned replies.

    `gate` is attached so the sample's model calls pass through the SAME privacy gate the live one
    uses. Without it the sample would be a preview of a product that does not check its own
    egress, which is the one thing this product is."""
    resolved = sample_settings(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": sample_reply(body)}}],
            "usage": {"prompt_tokens": 412, "completion_tokens": 96, "total_tokens": 508}})

    http = client or httpx.Client(base_url=resolved.base_url, timeout=5,
                                  transport=httpx.MockTransport(handler))
    return TokenFactory(resolved, http, audit=audit, boundary=gate)


# ---------------------------------------------------------------- the wiring -------------------
@dataclass
class Wiring:
    """Everything the v2 product is made of, in one object.

    Handed out as a whole rather than as a pile of fixtures because the privacy claim is about the
    RUN: the engine, the gate and the stores must be the same objects the tests inspect, or the
    evidence is about a different process."""

    mode: str
    service: V2Service
    engine: ChangeEngine
    routine: Routine
    boundary: BoundaryStore
    changesets: ChangeSetStore
    executor: Executor
    #: The ports the engine was actually handed. `mail`/`calendar` are the objects a caller can
    #: read back from (what the sample outbox holds, where the event ended up).
    mail: object
    calendar: object
    search: object
    llm: object
    audit: AuditLog
    connections: list[dict]
    #: Concept D. `library` is what the engine reads to decide whether a capability already exists;
    #: `learner` is the loop that mints one. Both are always present — the library is simply empty
    #: until the owner approves something.
    library: object = None
    learner: object = None
    refusals: list[str] = field(default_factory=list)
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    #: The care scenario's three objects, on the wiring for the same reason the others are: the
    #: claim is about the RUN, and a test that built its own care circle would be measuring a
    #: different circle from the one the gate consulted.
    facts: object = None
    care_circle: object = None
    digests: object = None
    #: Re-probes the providers and returns fresh connection rows. None when nothing can change.
    refresh: Callable[[], list[dict]] | None = None
    #: The allow/refuse record. On `Wiring` rather than only inside the engine because a caller
    #: reading the run back — a test, the redteam harness — needs the same object the engine wrote.
    decisions: object = None
    #: The owner credential this process minted, or None for the sample. On the wiring rather than
    #: only inside the service because `Wiring.session()` is a call the process makes ABOUT ITSELF
    #: — the CLI printing the session, the redteam harness reading it back — and a self-report
    #: that says "not the owner" while holding the token would be a confusing lie.
    owner_token: str | None = None
    #: What the owner agreed to. On `Wiring` rather than only inside the executor because the
    #: sample seeds it and a test reads it back — the same object the executor wrote, or the
    #: evidence is about a different store.
    agreements: object = None
    last_error: str = ""
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)

    @property
    def ready(self) -> bool:
        """True when every collaborator the mode needs is actually there."""
        return not self.refusals

    def session(self) -> dict:
        """`GET /v2/session`, with the connection rows re-read rather than remembered: a token
        that expired an hour ago must not still be reported as connected."""
        self.refresh_connections()
        return self.service.session(self.owner_token)

    def refresh_connections(self) -> list[dict]:
        if self.refresh is not None:
            self.connections = list(self.refresh())
            self.service.connections = tuple(self.connections)
        return self.connections

    # ---- the watcher ---------------------------------------------------------------------------
    def tick(self) -> list:
        """One poll of the authorized thread.

        A provider that has gone away is a state to report, not an exception to die of: the
        connection rows are re-read so the UI can say "Reconnect Google", and the tick returns
        nothing found. Every other failure propagates — a watcher that swallows its own bugs is a
        watcher nobody can trust."""
        try:
            found = self.routine.tick()
        except ReauthRequired as exc:
            self.last_error = str(exc)
            self.refresh_connections()
            return []
        self.refresh_connections()
        return found

    def start(self) -> "Wiring":
        """Check once now, then keep checking on the watcher's own bounded interval.

        One check at startup rather than waiting a full interval is what makes the product's
        "always on" observable: an app that has been open for two seconds should already know
        whether something needs the owner."""
        if self._thread is not None and self._thread.is_alive():
            return self
        if self.ready:
            self.tick()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"privatemesh-{self.mode}-routine")
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception as exc:                                  # noqa: BLE001
                # A background loop that dies is a routine that silently stopped being "always
                # on", which is exactly the lie the last-checked time exists to prevent.
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("v2 routine tick failed: %s", self.last_error)


#: Where the owner-held-out cases live. Resolved from the repository root rather than from the
#: package, because they are the OWNER's evidence about a generated skill: they ship beside the
#: package in this repo, they are never sent to a model, and `skillrt.run_harness` refuses the
#: arrangement where the code being graded can read them.
HELD_OUT_DIR = Path(__file__).resolve().parents[2] / "tests" / "heldout" / CAPABILITY

#: Where the held-out cases for TAUGHT skills live — a different directory from the one above
#: because it grades a different thing. `HELD_OUT_DIR` holds the owner's cases derived from
#: `fit.py`, and a taught rule deliberately disagrees with `fit.py`; grading a correct narrowing
#: skill against it would fail every case the rule narrows. What generalises across every taught
#: rule is the SAFETY contract — one option per offered slot, nothing the trusted rules refuse let
#: through, no venue invented — and that is what is in here.
TAUGHT_HELD_OUT_DIR = Path(__file__).resolve().parents[2] / "tests" / "heldout" / "taught"


class _ChangeResolver:
    """Answers "which change is this?" for the send path, and refuses to guess before it can.

    The reply's recipient is pinned to the change the owner approved, so the skill that sends needs
    to read that change. The engine holds changes and is built after the broker, which is built
    from the skills — a genuine cycle, broken the same way `DecisionLog.bind` breaks its own: build
    the resolver first, attach the engine second, and make the unattached state mean "no such
    change" rather than "any change". Unattached, the audience is empty and every send is refused;
    that is the safe direction, and `tests/test_inject_finding1_recipient_pin.py` pins it."""

    __slots__ = ("_engine",)

    def __init__(self) -> None:
        self._engine = None

    def attach(self, engine) -> None:
        self._engine = engine

    def get(self, change_id: str):
        if self._engine is None:
            return None
        return self._engine.get(change_id)


def _build(*, mail, calendar, search, llm, gate, boundary, audit, mode: str, owner_name: str,
           timezone: str, now, routine_interval: int, connections: list[dict],
           commitment_id: str | None, path: Path | None, refresh=None,
           refusals: list[str] | None = None, library=None, held_out: Path | None = None,
           learner=None, decisions=None, owner_token: str | None = None,
           facts=None, care_circle=None, digests=None) -> Wiring:
    """The half that is identical in both modes. Mode is a label here; the ports above are the
    difference, which is the whole design. The gate arrives already built because the MODEL needs
    it too, and there must be exactly one: two gates would mean two versions of one boundary."""
    approvals = ApprovalStore(path=_db(path, "approvals.db"))
    # The reply's audience is checked against the change the owner approved, and the engine is the
    # only thing that holds changes — but it is built below, because it needs the broker, which
    # needs this registry. The resolver is therefore attached afterwards, and until it is attached
    # it answers "no such change", which permits nobody. Same shape as `DecisionLog.bind`: the
    # two objects are genuinely mutually dependent, and the unattached state fails closed.
    changes = _ChangeResolver()
    # The APPROVED ARGUMENTS the two ports are bound to, held the same way and for the same reason:
    # the change-set store is built below, after the broker that is built from the skills. Separate
    # from the resolver above because the two answer different questions — "which change is this?"
    # and "what did the owner approve for it?" — and a port that could reach the first without the
    # second is a port that can check the audience and not the arguments. See `execute.ApprovedBundles`
    # for why the unattached state is a refusal.
    bundles = ApprovedBundles()
    registry = SkillRegistry(build_effect_skills(calendar, mail, gate=gate, decisions=decisions,
                                                 changes=changes, timezone=timezone,
                                                 bundles=bundles))
    broker = Broker(registry, audit, approvals=approvals)
    # ONE connection for the change records and the change sets, with the change-set file
    # ATTACHed. The review's High and Medium findings are both about writes that must not be able
    # to disagree: two processes doing last-writer-wins, and a crash between a change's version
    # bump and the bundle that version points at. Neither is fixable across two connections —
    # SQLite transactions are per-connection — so the two stores share one, and `_prepare` commits
    # the version, the bundle and the plan in a single transaction.
    conn = _store_conn(path)
    changesets = ChangeSetStore(approvals, conn=conn, schema="change_sets")
    library = library if library is not None else SkillLibrary(_db(path, "library.db"), now=now)
    learner = learner if learner is not None else Learner(
        llm=llm, library=library, now=now, held_out=held_out or HELD_OUT_DIR,
        # The gate travels with the learner for the same reason it travels with the model: the
        # owner's taught rule is owner text, and `start_taught` checks it against the boundary
        # before a thread or a model call exists. The model would refuse it at egress regardless;
        # checking it here is what makes the refusal free and attributable to the rule.
        taught_held_out=TAUGHT_HELD_OUT_DIR, gate=gate)
    #: The care circle, the protected facts and the digest ledger. Built here rather than passed in
    #: so BOTH modes get them — a live build that had no care circle would be a live build whose
    #: clause 7 refuses every care action, which is safe but is not the product.
    care_circle = care_circle if care_circle is not None else CareCircleStore(
        path=_db(path, "care.db"), now=now)
    facts = facts if facts is not None else ProtectedFactStore(path=_db(path, "facts.db"), now=now)
    digests = digests if digests is not None else DigestLedger(path=_db(path, "digests.db"), now=now)
    # The gate was built before these existed — the model needs it first — so it is pointed at them
    # here. Until this line the gate knows of no facts and no circle, and both of those fail
    # closed, so the window between the two is the refusing one rather than the open one.
    gate.bind_care(facts=facts, care_circle=care_circle)
    engine = ChangeEngine(mail=mail, calendar=calendar, search=GuardedSearch(search, gate),
                          llm=llm, gate=gate, boundary=boundary, broker=broker,
                          changesets=changesets, audit=audit, owner_name=owner_name,
                          timezone=timezone, commitment_id=commitment_id, now=now,
                          library=library, decisions=decisions,
                          care_circle=care_circle, digests=digests,
                          # The change records outlive the process for the same reason the
                          # watcher's cursor does: a restart that forgets the plan the owner was
                          # looking at has lost work, and `routine_seen` means the email that
                          # produced it will never be offered again. `:memory:` in the sample,
                          # where restarting IS the demonstration.
                          store=ChangeStore(conn=conn))
    changes.attach(engine)
    # The ports are bound to the plan the owner approved, and this is the moment that becomes
    # answerable: until the store is attached the resolver answers "no bundle", which refuses every
    # write rather than permitting one on a comparison that never happened.
    bundles.attach(changesets)
    #: The owner's standing answer, in its own file for the same reason the watcher's cursor has
    #: one: "the mode survives a restart" is the whole difference between a standing answer and a
    #: per-session accident. In the sample it is `:memory:`, where restarting IS the demonstration.
    #: Built BEFORE the executor, which re-reads it immediately before each effect — one object, so
    #: the mode a run was authorized under and the mode it is re-checked against cannot be two rows.
    limits = LimitStore(path=_db(path, "limits.db"), now=now)
    #: What the owner agreed to, in its own file for the same reason the standing answer has one:
    #: "the product remembers what you agreed to across sessions" is a claim about a restart, and a
    #: store that only lived as long as the process would make it a claim about a session. In the
    #: sample it is `:memory:`, where restarting IS the demonstration.
    agreements = AgreementStore(path=_db(path, "agreements.db"), now=now)
    executor = Executor(broker=broker, changesets=changesets, calendar=calendar, mail=mail,
                        boundary=boundary, audit=audit, decisions=decisions,
                        limits=limits, changes=engine, agreements=agreements,
                        # The SAME gate, circle and ledger the engine holds. The digest path is a
                        # send path, and a second gate would be a second set of protected facts to
                        # keep in step — with the one that fell behind being the one that leaked.
                        gate=gate, care_circle=care_circle, digests=digests,
                        # The owner's configured zone, so the digest's day window is built at the
                        # owner's offset rather than the host's — the same value the effect skills
                        # are handed, from the same place.
                        timezone=timezone)
    routine = Routine(engine, mail, path=_db(path, "routine.db"), interval_seconds=routine_interval,
                      now=now)
    service = V2Service(engine=engine, routine=routine, boundary=boundary, changesets=changesets,
                        executor=executor, calendar=calendar, mode=mode, owner_name=owner_name,
                        timezone=timezone, connections=connections, now=now, library=library,
                        learner=learner, owner_token=owner_token, limits=limits,
                        agreements=agreements,
                        # The sample's owner is the person running the sample, and that is the
                        # ONLY claim this boolean still carries: `V2Service.is_owner` reads it
                        # only when there is no token AND the mode is the sample. A live build
                        # mints a token, so no boolean can open its owner-only routes.
                        authenticated_owner=(owner_token is None and mode == SAMPLE_MODE),
                        facts=facts, care_circle=care_circle, digests=digests)
    return Wiring(mode=mode, service=service, engine=engine, routine=routine, boundary=boundary,
                  changesets=changesets, executor=executor, mail=mail, calendar=calendar,
                  search=search, llm=llm, audit=audit, connections=list(connections),
                  refusals=list(refusals or []), interval_seconds=routine_interval,
                  refresh=refresh, library=library, learner=learner, decisions=decisions,
                  owner_token=owner_token, agreements=agreements, facts=facts,
                  care_circle=care_circle, digests=digests)


def _db(path: Path | None, name: str) -> str:
    """One file per store under the data dir, or `:memory:` when there is no data dir.

    The sample keeps everything in memory on purpose: a judge who restarts the sample must see
    the same change waiting for them, and a persisted watcher cursor would have already read it.
    Live mode gets files, because there "the same email twice" is a real inbox and not a script.
    """
    if path is None:
        return ":memory:"
    path.mkdir(parents=True, exist_ok=True)
    return str(path / name)


def owner_token_for_live() -> str:
    """The owner's token for a live process: mint it once, then hand back the same one.

    The third review's Critical is that this used to be a BOOLEAN — `build_live(owner_session=
    True)` — which meant every request reaching the port was the owner's, with no credential, no
    session and no caller identity. A boolean is something the code says about itself; a token is
    something the caller has to present. `owner_token.issue` is where the reading and writing
    lives, so the path and the file mode are stated in exactly one module.
    """
    from .owner_token import issue

    return issue()


def _store_conn(path: Path | None) -> sqlite3.Connection:
    """The connection the change records and the change sets share.

    Two stores, one connection, because the guarantee being bought is that a change's version and
    the bundle that version points at are committed together. The change-set file is ATTACHed
    rather than merged into one file: both files stay where they are, an existing `changes.db` and
    `changesets.db` keep their data, and a transaction still spans both. With no data dir the
    sample shares one in-memory database, which is the same guarantee for the same reason.

    `check_same_thread=False` because the API serves from a thread pool; the ordering that matters
    is the database's, which `txn.transaction` provides."""
    if path is None:
        conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=5.0)
        conn.execute("ATTACH DATABASE ':memory:' AS change_sets")
        return conn
    conn = sqlite3.connect(_db(path, "changes.db"), check_same_thread=False, timeout=5.0)
    conn.execute("ATTACH DATABASE ? AS change_sets", (_db(path, "changesets.db"),))
    return conn


def _now_fn(now):
    """A callable clock, from a string, a datetime, a callable, or nothing.

    Every collaborator here takes `now` as a callable and calls it repeatedly, so the wiring
    normalises once. Injecting a fixed instant is how the whole suite reproduces a day; letting
    one collaborator receive a datetime and another a function is how two of them end up
    disagreeing about what time it is."""
    if callable(now):
        return now
    if isinstance(now, str):
        moment = _dt.datetime.fromisoformat(now)
    elif isinstance(now, _dt.datetime):
        moment = now
    else:
        return lambda: _dt.datetime.now().astimezone()
    return lambda: moment


# ---------------------------------------------------------------- sample -----------------------
def sample_connections(ports: SamplePorts, llm=None) -> list[dict]:
    """Four rows, four honest labels. The model row is added here because the simulators only know
    about the three provider ports they implement.

    The model row is DERIVED from the model object this wiring was actually handed rather than
    typed beside it. A literal here is how the startup banner came to tell an operator the model
    was a scripted Token Factory while the session payload on the same server said it was real —
    `build_demo_world` builds through this function, and a demo world's model is the real one. The
    derivation lives in `demo_session.model_truth` so that the row and the payload's
    `real`/`simulated` lists are one reading of one object; the lazy import is the same shape
    `Wiring`'s builder already uses to reach that module."""
    from .demo_session import model_truth
    return list(ports.connections()) + [model_truth(llm).row()]


def build_sample(*, settings: Settings | None = None, ports: SamplePorts | None = None,
                 commitment: ProtectedCommitment | None = None, llm=None, audit: AuditLog | None = None,
                 owner_name: str = SAMPLE_OWNER_NAME, timezone: str = SAMPLE_TIMEZONE,
                 now=None, routine_interval: int = DEFAULT_INTERVAL_SECONDS,
                 path: str | Path | None = None, library=None, held_out=None,
                 care_week: bool = False,
                 mode: str = SAMPLE_MODE, owner_token: str | None = None) -> Wiring:
    """The sample product: the real engine, deterministic provider simulators, nothing to connect.

    `llm` may be a model or a CALLABLE taking the gate. The callable form is how a test observes
    what the model was asked: it builds its own Token Factory over a recording transport, answers
    from `sample_reply`, and attaches the gate this function built — so the observed run is the
    gated run. Observing the script is not the same as replacing it.

    `path` is None by default, which keeps the sample entirely in memory for the reason above. A
    caller that passes a directory gets a PERSISTENT library instead — that is how a test shows the
    product's own claim, that a skill learned once is still there in a second app instance."""
    # `care_week` swaps the whole day rather than adding to it. The scheduling sample is one
    # meeting with one friend; adding four providers and a second inbox to that day would change
    # what every existing sample test is measuring, and a fixture that quietly widens the thing
    # under test is worse than a fixture that is opt-in.
    if ports is None and care_week:
        ports = build_care_week_ports()
    ports = ports or build_sample_ports(SAMPLE_DATE)
    audit = audit or AuditLog(":memory:")
    boundary = BoundaryStore()
    protected = commitment or ports.protected
    boundary.put(id=protected.id, start=protected.start, end=protected.end,
                 buffer_minutes=protected.buffer_minutes, title=protected.title,
                 location=protected.location, reason=protected.reason,
                 attendees=protected.attendees)
    clock = _now_fn(now or SAMPLE_NOW)
    decisions = _decision_log(clock)
    gate = PrivacyGate(boundary, audit=audit, decisions=decisions)
    bind_decisions(decisions, gate)
    model = llm(gate) if callable(llm) else (llm or sample_model(settings, audit=audit, gate=gate))
    wiring = _build(mail=ports.mail, calendar=ports.calendar, search=ports.search, llm=model,
                    gate=gate, boundary=boundary, audit=audit, mode=mode,
                    owner_name=owner_name, timezone=timezone, now=clock,
                    routine_interval=routine_interval, connections=sample_connections(ports, model),
                    commitment_id=protected.id,
                    path=Path(path).expanduser() if path is not None else None,
                    library=library, held_out=held_out, decisions=decisions,
                    # No token by default, on purpose: the sample has no providers to reach and
                    # nothing to configure, and a judge who has to find a token file before the
                    # decision log renders has been given a worse demo for no security. `_build`
                    # reads the absence of a token TOGETHER WITH the mode, so this is a claim
                    # about the sample and not a default that a live build inherits.
                    #
                    # A DEMO world is the one caller that passes one, and it is not an exception to
                    # that rule but an instance of it: the token is generated by the server, never
                    # reaches the visitor, and is scoped to a world that exists for half an hour
                    # and holds nothing the visitor did not put there. It is what lets the demo
                    # read its own decisions and model view through the REAL gate instead of
                    # around it — a bypass would be the thing that quietly outlives the session.
                    owner_token=owner_token)
    _seed_agreements(wiring, clock, day=ports.calendar.day)
    _seed_care(wiring, clock, day=ports.calendar.day)
    return wiring


def _seed_care(wiring: Wiring, clock, *, day: str) -> None:
    """Give the sample its care circle and its protected facts, through the OWNER's own door.

    Every row goes in through `CareCircleStore.add(by="owner")` and `ProtectedFactStore.add(...)`,
    which are the same calls `POST /v2/care-circle` makes. Seeding the tables directly would let
    the fixtures be a shape the store cannot actually produce — a member with no `added_by`, a fact
    whose kind has no placeholder — and the sample would then demonstrate a list the product cannot
    really hold.

    The facts are the interesting half. The moment they are stored, the gate refuses any prompt or
    payload carrying them, so every screen the sample renders from here on has already been through
    the check the feature exists for. A sample that only LOOKED private would be the same engine
    with a caregiver skin, which is the specific failure this lane was raised to avoid.
    """
    from . import care as care_rules
    from .sim import sample_care_circle, sample_protected_facts

    if wiring.care_circle is not None and not wiring.care_circle.members():
        for row in sample_care_circle():
            wiring.care_circle.add(name=row["name"], email=row["email"],
                                   may_receive=row["may_receive"], by=care_rules.OWNER,
                                   added_at=clock().isoformat(timespec="seconds"))
    if wiring.facts is not None and not wiring.facts.facts():
        for row in sample_protected_facts():
            wiring.facts.add(label=row["label"], kind=row["kind"], value=row["value"],
                             by=care_rules.OWNER,
                             created_at=clock().isoformat(timespec="seconds"))


# ---------------------------------------------------------------- demo --------------------------
def demo_settings(base: Settings | None = None) -> Settings:
    """The demo's settings: the REAL model key and model ids, and no provider key at all.

    The inverse of `sample_settings`, deliberately. There, the key is a placeholder because the
    model is scripted; here the key is the genuine article because the model is the one thing that
    is real, and `tavily_key` is emptied because web research is one of the simulated parts.

    A missing `NEBIUS_API_KEY` refuses here rather than at the first model call: a demo that starts
    and then 500s in front of a judge has spent the judge's patience to report a fact the operator
    could have been told at startup."""
    base = base or Settings.load()
    if not base.api_key:
        raise DemoRefused(
            "demo mode needs a model key, because the model is the one real thing in it: set "
            "NEBIUS_API_KEY (see .env.example). For a run with no key and no network at all, use "
            "--sample, which is fully simulated.")
    filled = {name: (getattr(base, name) or fallback)
              for name, fallback in DEMO_MODEL_DEFAULTS.items()}
    return dataclasses.replace(base, tavily_key="", **filled)


def build_demo_world(*, sid: str, settings: Settings, caps, budget, global_ledger,
                     client: httpx.Client | None = None) -> Wiring:
    """One visitor's world: the sample's simulated providers, the real model, this visitor's caps.

    Built through `build_sample` rather than beside it, so the demo's engine, boundary, approval
    store, hash-chained audit and seeded agreements are the sample's own objects. What differs is
    exactly three things, and all three are arguments: the `mode` (so `GET /v2/session` says
    `demo`, and so `is_owner` refuses every owner route), the model, and the DAY.

    `care_week=True` is the third, and it is not decoration: without it a visitor was handed the
    scheduling sample — one meeting with one friend in a café — while every screen on the page
    promised a simulated CAREGIVER inbox. The week is what makes the page's own words true: the
    inbox the visitor edits holds the care circle's four providers, and the lookalike
    "benefits update" email (`sim.caregiver_messages`, the same message
    `redteam/cases/09-lookalike-benefits-update.json` delivers) arrives as ordinary mail beside a
    refill to confirm and a pickup to rebook. `_seed_care` has already put the circle and the
    protected facts in this world by the time it is built, so the care guard the visitor reads a
    refusal from is refusing about THIS session's own stored facts.

    The model is handed in as the CALLABLE form `build_sample` documents, which is what lets it be
    built from the gate `build_sample` itself created. The privacy boundary is per session — its
    `BoundaryStore` holds this visitor's protected commitment — so a model built outside would be
    a model guarding a different world.

    Every store is `:memory:` and `path` is left None, so "discarded when the session ends" is
    true by construction: there is no file, and dropping the object drops everything in it. The
    care circle and the protected facts are per-session for the same reason and by the same
    mechanism: they are rows in this world's `:memory:` stores, so one visitor's added member is
    invisible to the next, and nothing the visitor does to either outlives their half hour."""
    from .demo_session import ChainedLedger, DemoModel
    from .llm import TokenFactory
    from .spend import Caps as SpendCaps
    from .spend import SpendLedger

    # Belt and braces on the startup refusal: `build_demo` refuses the process, and this refuses
    # each world. The cost of the second check is one `os.environ` read per session, and it is what
    # makes the claim hold for a world built by something that did not go through the CLI.
    if real_provider_credentials():
        raise DemoRefused(
            "refusing to build a demo world in a process that has real provider credentials "
            "configured — see demo_session.real_provider_credentials.")

    resolved = demo_settings(settings)
    #: This visitor's own ledger, in memory and capped at the session budget. Both its total and
    #: its daily cap are the session cap: a session lives far less than a day, so a second daily
    #: number would be a knob that can never bind.
    session_ledger = SpendLedger(":memory:", caps=SpendCaps(caps.session_usd, caps.session_usd))
    chained = ChainedLedger(session_ledger, global_ledger)
    audit = AuditLog(":memory:")

    def make_model(gate):
        return DemoModel(
            # `client` is the test seam, and the same one `sample_model` and `conftest.FakeTF`
            # already use: a caller that injects a transport gets a real `TokenFactory` over a
            # MockTransport, which is how a test counts what reached the provider without reaching
            # one. The spend chain is attached either way — that is the thing under test.
            real=TokenFactory(resolved, client, audit=audit, boundary=gate, spend=chained),
            # The scripted path is the sample's own model, built the sample's own way, over the
            # same gate. Degrading to it is therefore literally "the sample path", not a thinner
            # second implementation of it that could answer differently.
            scripted=sample_model(resolved, audit=audit, gate=gate),
            budget=budget, caps=caps)

    # The session's own credential. Generated here, held by the server, presented by the demo's
    # own handlers when they read this world's decisions — never sent to the visitor, and dead
    # with the session. It is what makes the demo read through the real gate rather than around it.
    return build_sample(settings=resolved, audit=audit, llm=make_model, mode=DEMO_MODE,
                        care_week=True, owner_token=secrets.token_urlsafe(32))


def build_demo(*, settings: Settings | None = None, caps=None,
               client: httpx.Client | None = None) -> tuple:
    """The demo HOST: the process-level service the routes are mounted on, and the manager behind it.

    Returns two objects, and the pair is the shape of the mode. The HOST is one ordinary v2 service
    whose only usable routes are the four demo ones — every owner route on it refuses, because
    `mode` is `demo` and `is_owner` answers False for every caller without the owner token, and the
    host has none to present. The MANAGER owns the sessions, and each session is its own world with
    its own real model, budget and `:memory:` stores.

    The host is built by `build_sample` and that is deliberate rather than lazy: it means the demo
    process is a sample process with one field set, so there is no second composition root to keep
    in step with the product. The host's own model is never called by anything — the routes all
    reach through to a session — and it is the sample's scripted one, so even a bug that called it
    would reach a MockTransport rather than the owner's card.

    The startup refusal happens HERE, once, before anything is served: see
    `demo_session.refuse_if_providers_configured` and `demo_session.refuse_if_multi_worker` — the
    second is pm-demo-r3 finding 3's first half. The session table this call builds
    (`DemoSessions`) is process-local memory, so a deployment running this app under more than one
    worker process would give each worker its own copy of every cap the operator set, silently
    multiplying them; the safe answer is to refuse rather than serve a demo whose printed caps are
    not the caps actually in effect."""
    from .demo_session import DemoCaps, DemoSessions, real_model_truth

    refuse_if_providers_configured()
    refuse_if_multi_worker()
    resolved = demo_settings(settings)
    manager = DemoSessions(settings=resolved, caps=caps or DemoCaps.from_env())
    if client is not None:
        # The test seam, threaded through to every world builder the manager makes.
        manager._build_world = lambda **kw: build_demo_world(**kw, client=client)
    host = build_sample(settings=resolved, mode=DEMO_MODE)
    host.service.demos = manager
    # The host's own model row describes the model its SESSIONS call, not the scripted one the
    # host itself holds. `build_sample` above labelled the row from the object it was given — a
    # sample model, which is what the host's own `llm` is — and the host's model is never called
    # by anything, so that row was true about an object nobody uses and false about the process an
    # operator is looking at. The banner read "connected — sample model replies (synthetic)" while
    # the session payload one screen away said the model was real, and the spend ledger proved the
    # payload right. The replacement is the same `ModelTruth` a world's own row comes from, built
    # from the SAME `resolved` settings every world's `TokenFactory` is built from.
    #
    # `host.connections` and `host.service.connections` are set together because they are the two
    # readers: the CLI banner prints the first, `GET /v2/session` serves the second. A demo host
    # has no `refresh`, so nothing rewrites them afterwards.
    truth = real_model_truth(resolved.model_reason)
    updated = [truth.row() if row["service"] == truth.service else row
               for row in host.connections]
    host.connections = updated
    host.service.connections = tuple(updated)
    return manager, host


def _seed_agreements(wiring: Wiring, clock, *, day: str) -> None:
    """Give the sample two agreements, then run the real check over them.

    The fixtures are the day's own events — one as it stands, one an hour away from where it now
    reads — and they are seeded UNCHECKED and then CHECKED, rather than seeded with a status typed
    in beside them. That order is the whole point: the status the sample serves is one the real
    checker produced by reading the real calendar, so the fixtures cannot quietly disagree with the
    day they are supposed to describe. A caller that hands in its own ports gets its own honest
    answer, which may be `unchecked` — the sample's job is to be runnable, not to be right about a
    calendar somebody else built.

    An event the sample day does not contain does not stop the app starting: `check` treats a read
    that fails as an answer, so the fixture lands as `unchecked` with the reason. What this does
    still raise for is a row this build cannot parse, which `AgreementStore.save` refuses to
    overwrite — loud on purpose, and the one case where a broken store SHOULD stop a start.
    """
    if wiring.agreements is None:
        return
    for agreement in sample_agreements(day):
        wiring.agreements.save(agreement)
    AgreementChecker(wiring.agreements, wiring.calendar, now=clock).check()


# ---------------------------------------------------------------- live -------------------------
def probe_google(provider, account: str) -> tuple[str, str]:
    """(state, detail) for the Google rows. Never raises: this runs while an app is starting, and
    "the token is gone" is an answer, not a failure."""
    try:
        provider.access_token()
    except ReauthRequired as exc:
        state = "disconnected" if _stored_token(provider) is None else "expired"
        return state, str(exc)
    except Exception as exc:                                          # noqa: BLE001
        return "disconnected", f"{type(exc).__name__}: {exc}"
    return "connected", f"Google account '{account}' is connected"


def _stored_token(provider):
    load = getattr(getattr(provider, "store", None), "load", None)
    if not callable(load):
        return None
    try:
        return load()
    except Exception:                                                 # noqa: BLE001
        return None


def load_commitment(path: str | Path) -> ProtectedCommitment | None:
    """The owner's protected commitment, from a JSON file.

    It is read from a file outside the repo rather than typed into a command line, because the
    title, the place and the reason are the most private strings this product holds and a shell
    history is not a private place."""
    p = Path(path).expanduser()
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    return ProtectedCommitment(id=str(raw["id"]), start=str(raw["start"]), end=str(raw["end"]),
                               buffer_minutes=int(raw.get("buffer_minutes") or 0),
                               version=int(raw.get("version") or 1),
                               title=str(raw.get("title") or ""),
                               location=str(raw.get("location") or ""),
                               reason=str(raw.get("reason") or ""),
                               attendees=tuple(raw.get("attendees") or ()))


def commitment_path(data_dir: Path | None) -> Path:
    override = os.environ.get("PM_PROTECTED_COMMITMENT")
    if override:
        return Path(override).expanduser()
    return (data_dir or Path("data") / "v2") / "protected.json"


def build_live(*, settings: Settings | None = None, account: str = "you",
               http: httpx.Client | None = None, google=None, commitment: ProtectedCommitment | None = None,
               # "" and not "Owner": see `ChangeEngine.__init__`'s own default for why a name
               # nobody configured must not render as the literal word "Owner".
               audit: AuditLog | None = None, owner_name: str = "",
               timezone: str = SAMPLE_TIMEZONE, now=None, spend: SpendLedger | None = None,
               data_dir: str | Path | None = None, label: str = DEFAULT_LABEL,
               routine_interval: int = DEFAULT_INTERVAL_SECONDS,
               owner_token: str | None = None) -> Wiring:
    """The live product: real connectors, real Token Factory, real spend ledger.

    It is built even when a credential is missing — the connections say so, and `require_ready` is
    what refuses to serve. That split is deliberate: one caller (the CLI) must refuse, another
    (the UI) must be able to SAY WHY."""
    settings = settings or Settings.load()
    directory = Path(data_dir).expanduser() if data_dir is not None else None
    audit = audit or AuditLog(_db(directory, "audit.db"))
    boundary = BoundaryStore(path=_db(directory, "boundary.db"))
    owner_commitment = commitment or load_commitment(commitment_path(directory))
    if owner_commitment is not None and boundary.current(owner_commitment.id) is None:
        # Seeded once. After that the STORE is the truth: re-reading the file on every start
        # would mint a new boundary version and make every reviewed proposal stale for no reason.
        boundary.put(id=owner_commitment.id, start=owner_commitment.start,
                     end=owner_commitment.end, buffer_minutes=owner_commitment.buffer_minutes,
                     title=owner_commitment.title, location=owner_commitment.location,
                     reason=owner_commitment.reason, attendees=owner_commitment.attendees)
    google = google or GoogleAuth(account, http=http)
    state, detail = probe_google(google, account)
    mail = GmailClient(account, http=http, tokens=google, label=label)
    calendar = GCalClient(account, http=http, tokens=google)
    search = TavilySearch(settings.tavily_key, http=http)

    refusals: list[str] = []
    if state != "connected":
        refusals.append(detail)
    if not settings.tavily_key:
        refusals.append("TAVILY_API_KEY is not set — venue research cannot run")
    if not settings.api_key:
        refusals.append("NEBIUS_API_KEY is not set — the model cannot run")
    if boundary.current_all() == []:
        refusals.append(f"no protected commitment is configured — the plan has no boundary to "
                        f"respect; write one to {commitment_path(directory)} "
                        f"(or set PM_PROTECTED_COMMITMENT)")

    def refresh() -> list[dict]:
        current_state, current_detail = probe_google(google, account)
        rows = [{"service": "gmail", "state": current_state, "detail": current_detail},
                {"service": "calendar", "state": current_state, "detail": current_detail},
                _key_row("tavily", settings.tavily_key, "TAVILY_API_KEY"),
                _key_row("nemotron", settings.api_key, "NEBIUS_API_KEY")]
        return rows

    rows = refresh()
    decisions = _decision_log(_now_fn(now))
    gate = PrivacyGate(boundary, audit=audit, decisions=decisions)
    bind_decisions(decisions, gate)
    llm = (TokenFactory(settings, http, audit=audit, spend=spend or SpendLedger(), boundary=gate)
           if settings.api_key else UnavailableModel("NEBIUS_API_KEY is not set"))
    return _build(mail=mail, calendar=calendar, search=search, llm=llm, gate=gate,
                  boundary=boundary, audit=audit, mode=LIVE_MODE, owner_name=owner_name,
                  timezone=timezone, now=_now_fn(now), routine_interval=routine_interval,
                  connections=rows,
                  commitment_id=(owner_commitment.id if owner_commitment else None),
                  path=directory, refresh=refresh, refusals=refusals, decisions=decisions,
                  # Minted here, once, at startup, into `~/.privatemesh/owner.token` (0600), or
                  # wherever PM_OWNER_TOKEN_FILE points. The server writes it and the owner reads
                  # it; nothing else in the process needs it, which is why it is not on the
                  # session payload and never appears in a log.
                  owner_token=owner_token if owner_token is not None else owner_token_for_live())


def _decision_log(clock) -> "DecisionLog":
    """One decision log, with the engine's clock. The gate's text check is bound to it by the
    caller once the gate exists — `bind_decisions` below, which is the only supported way.

    Building it here rather than inside `_build` is what lets the log be handed to the GATE as
    well: the gate is constructed before `_build` runs (the model needs it), so a log built inside
    would arrive after the object that has to write refusals into it."""
    from .decisions import DecisionLog

    return DecisionLog(now=clock)


def bind_decisions(decisions, gate) -> None:
    """Point the log's detail-scrubber at the gate's text check.

    A one-line function rather than a `bind(gate.check_text)` call because `check_text` takes
    `kind` as a REQUIRED keyword-only argument: passing the bound method directly raises
    `TypeError` on every call, and `DecisionLog._scrub` catches broadly, so the failure mode is
    not a crash — it is every decision silently reporting "detail withheld". A scrubber that
    refuses everything looks exactly like a scrubber that works, which is why this is a named
    function with a test rather than a lambda at each call site."""
    decisions.bind(lambda text: gate.check_text(text, kind="decision"))


def _key_row(service: str, key: str, env: str) -> dict:
    if key:
        return {"service": service, "state": "connected", "detail": f"{env} is set"}
    return {"service": service, "state": "disconnected", "detail": f"{env} is not set"}


def build(mode: str = SAMPLE_MODE, **kw) -> Wiring:
    """The one entry point a host should call. `sample` needs nothing but the code; `live` needs
    credentials, and says which ones are missing rather than raising."""
    if mode == SAMPLE_MODE:
        return build_sample(**kw)
    if mode == LIVE_MODE:
        return build_live(**kw)
    if mode == DEMO_MODE:
        # A demo is not one world but one world PER VISITOR, so there is nothing here for a
        # single `build` to return. `demo_session.DemoSessions` is the entry point; naming it
        # rather than raising a bare "unknown mode" is the difference between a caller who reads
        # one line and a caller who reads the whole module to find out what they missed.
        raise ValueError(
            f"'{DEMO_MODE}' is not a single wiring: a demo session is one world per visitor. Use "
            f"demo_session.DemoSessions(...).start(client), or wiring.build_demo_world(...) for "
            f"one session's world.")
    raise ValueError(f"unknown v2 mode {mode!r} — expected '{SAMPLE_MODE}', '{DEMO_MODE}' or "
                     f"'{LIVE_MODE}'")


def require_ready(wired: Wiring) -> None:
    """Refuse to serve a live product that cannot reach its providers. Called by the CLI, which
    is the only place allowed to make "start anyway" impossible."""
    if wired.ready:
        return
    raise ProviderUnavailable("live mode is not ready:\n  - " + "\n  - ".join(wired.refusals))
