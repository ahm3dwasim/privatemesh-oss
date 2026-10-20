"""The public demo: a real model, simulated providers, and a cap that cannot be talked past.

A third v2 mode, `demo`, sits between `sample` and `live` and is deliberately the inverse of each:

* **the model is real.** A real `TokenFactory` over a real socket to Token Factory — the same tier
  router, the same egress sanitizer, the same privacy boundary, the same ledger arithmetic. A judge
  watching the demo work is watching the product work, not a recording of it.
* **the providers are simulated.** The sample's in-memory mail, calendar and search, seeded per
  visitor. There is no connector here to misconfigure: the demo holds `SamplePorts` and nothing
  else, so "the demo cannot reach the owner's inbox" is a fact about which objects exist rather
  than a rule someone has to remember to apply. `real_provider_credentials` is the second half of
  that — a demo that starts on a machine that HAS a Google token is a demo one refactor away from
  using it, so it refuses to start at all.
* **nothing outlives the visitor.** Each session is its own `Wiring` over `:memory:` stores. The
  owner's data directory, audit log, memory store and agreements are not merely unread — they are
  never handed to anything the session can reach.

## The caps, and why the refusal is a degradation

Four limits stand between a stranger and the owner's card, and all four are read from the
environment with a **safe default**: a setting that is missing, unreadable, zero, negative or
past its absolute ceiling resolves to the *small* value. The failure mode of a typo is a demo that
does less, never a demo that spends without limit.

1. `session_usd`   — what one visitor's model calls may cost, enforced by the existing
                     `SpendLedger` (`spend.py`) rather than a second counter of our own.
2. `session_calls` — how many model calls one visitor may make.
3. `rate`          — how often one client may make a model call, via the playground's existing
                     bounded `TokenBucket`.
4. `total`/`daily` — the whole demo's ceiling, on a ledger of its own so the demo cannot eat the
                     owner's real budget, reserved through the same code as (1).

When one is reached the demo **degrades**: the run completes against the scripted sample path and
says so, rather than 500ing at a judge or silently answering from a different model. "You have
used your budget; this answer is the scripted one" is a true sentence a stranger can act on, and
it is the whole reason the caps can be this tight.
"""
from __future__ import annotations

import dataclasses
import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import google_auth, playground
from .config import Settings
from .spend import Caps, SpendCapReached, SpendLedger

#: The third mode. A claim about the providers, exactly like `sample` and `live` — see `wiring`.
DEMO_MODE = "demo"

# --- the caps, and their safe defaults -----------------------------------------------------------
#
# Every default below is the SMALL end of what would still work. A demo is a public URL that spends
# a private card, so the value that has to be argued for is the larger one, not the smaller.

#: One visitor's model spend. MEASURED, not guessed. One full run of the demo (detect, prepare, and
#: the refusal that follows) is **six REASON-tier calls** costing **$0.001453** settled, measured
#: against the real provider; the WORST CASE each call reserves before it is sent — prompt at
#: ~$0.30/M in plus `max_tokens` at ~$0.90/M out — sums to **$0.0128** for the run.
#:
#: The cap binds on the RESERVATION rather than on the settled total, so this is set to cover four
#: such runs and no more; the call cap below is the one that normally binds first, which is what
#: the docstring says and what this number is chosen to make true. Four runs settle at $0.0058 —
#: a ninth of this cap — so this is the backstop for a mispriced model or a runaway prompt, not the
#: limit a visitor meets. `README.md` carries the table.
DEFAULT_SESSION_USD = 0.05
#: One visitor's model calls. The tighter of the two session limits in practice, and the one that
#: makes the demo's cost predictable in calls rather than in tokens. Four full runs — measured, not
#: assumed: a run is six calls, this cap stops the 25th, and a session's fourth run therefore
#: completes against the real model while its fifth degrades with reason `calls`. Enough for a
#: judge to attack it, watch it hold, edit the email differently and attack it again, which is the
#: behaviour the mode exists to invite.
DEFAULT_SESSION_CALLS = 24
#: One client's model calls per window, and the size of the bucket's table. The table bound matters
#: as much as the rate: the key is caller-determined, so an unbounded table is a memory leak with a
#: rate limit's name.
DEFAULT_RATE_REQUESTS = 10
DEFAULT_RATE_WINDOW = 60.0
DEFAULT_RATE_MAX_CALLERS = 512
#: The whole demo. On its own ledger file, so exhausting the demo does not disable the owner's real
#: product and vice versa.
DEFAULT_TOTAL_USD = 1.00
DEFAULT_DAILY_USD = 0.25
#: How many live sessions the process will hold, and how long one lives. Both bounded because a
#: session is memory a stranger asked for: without these, "start a session" is an allocation
#: primitive that needs no credential.
DEFAULT_MAX_SESSIONS = 256
DEFAULT_SESSION_TTL = 1800.0

#: Absolute ceilings. A setting above one of these is treated as unreadable. This is what makes
#: "never unlimited" a property of the code rather than a convention about the environment: the
#: largest demo these knobs can describe is still a bounded, cheap one.
CEILING_SESSION_USD = 1.00
CEILING_SESSION_CALLS = 50.0
CEILING_RATE_REQUESTS = 600.0
CEILING_TOTAL_USD = 5.00
CEILING_DAILY_USD = 1.00
CEILING_MAX_SESSIONS = 4096.0
CEILING_SESSION_TTL = 86400.0

#: The environment names a real PROVIDER credential can arrive under. The model key is deliberately
#: absent: the model is the one real dependency demo mode is supposed to have.
PROVIDER_CREDENTIAL_ENV = ("TAVILY_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
#: Imported from the adapter that actually reads them, not re-typed: `google_auth.
#: CREDENTIAL_DEFAULT_PATHS` is the adapter's own list of where a real credential may live, and a
#: local copy here is exactly how these two drifted apart once already — see that constant's own
#: comment. `test_the_checked_google_paths_are_exactly_the_adapters_own_defaults` in
#: `tests/test_demo_mode.py` is the guard: it fails the day a THIRD path is added to the adapter
#: and this module is not updated to check it too.
DEFAULT_GOOGLE_CLIENT, DEFAULT_GOOGLE_TOKEN_DIR = google_auth.CREDENTIAL_DEFAULT_PATHS


def _bounded(raw: str | None, fallback: float, ceiling: float) -> float:
    """One setting, read so that every failure mode resolves to the SMALL value.

    Missing, not a number, zero, negative, `nan`, infinite, and above `ceiling` all return
    `fallback`. The two that need the comparisons below rather than a simple range check are
    `nan` — which is a float, and for which every comparison is false, so `not (value > 0)` is how
    it is caught — and `inf`, which is caught by the ceiling."""
    try:
        value = float(raw)                                    # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not (value > 0) or value > ceiling:
        return fallback
    return value


@dataclasses.dataclass(frozen=True)
class DemoCaps:
    """Every limit the demo runs under, resolved once at startup from the environment."""

    session_usd: float = DEFAULT_SESSION_USD
    session_calls: int = DEFAULT_SESSION_CALLS
    rate_requests: int = DEFAULT_RATE_REQUESTS
    rate_window: float = DEFAULT_RATE_WINDOW
    rate_max_callers: int = DEFAULT_RATE_MAX_CALLERS
    total_usd: float = DEFAULT_TOTAL_USD
    daily_usd: float = DEFAULT_DAILY_USD
    max_sessions: int = DEFAULT_MAX_SESSIONS
    session_ttl: float = DEFAULT_SESSION_TTL

    @classmethod
    def from_env(cls, env=None) -> "DemoCaps":
        """Read the caps, defaulting every one of them to the small value. See `_bounded`."""
        e = os.environ.get if env is None else env.get
        return cls(
            session_usd=_bounded(e("PM_DEMO_SESSION_USD"), DEFAULT_SESSION_USD, CEILING_SESSION_USD),
            session_calls=int(_bounded(e("PM_DEMO_SESSION_CALLS"), DEFAULT_SESSION_CALLS,
                                       CEILING_SESSION_CALLS)),
            rate_requests=int(_bounded(e("PM_DEMO_RATE_REQUESTS"), DEFAULT_RATE_REQUESTS,
                                       CEILING_RATE_REQUESTS)),
            rate_window=_bounded(e("PM_DEMO_RATE_WINDOW"), DEFAULT_RATE_WINDOW,
                                 CEILING_SESSION_TTL),
            rate_max_callers=int(_bounded(e("PM_DEMO_RATE_MAX_CALLERS"), DEFAULT_RATE_MAX_CALLERS,
                                          CEILING_MAX_SESSIONS)),
            total_usd=_bounded(e("PM_DEMO_TOTAL_USD"), DEFAULT_TOTAL_USD, CEILING_TOTAL_USD),
            daily_usd=_bounded(e("PM_DEMO_DAILY_USD"), DEFAULT_DAILY_USD, CEILING_DAILY_USD),
            max_sessions=int(_bounded(e("PM_DEMO_MAX_SESSIONS"), DEFAULT_MAX_SESSIONS,
                                      CEILING_MAX_SESSIONS)),
            session_ttl=_bounded(e("PM_DEMO_SESSION_TTL"), DEFAULT_SESSION_TTL,
                                 CEILING_SESSION_TTL),
        )

    def as_dict(self) -> dict:
        """What the visitor is shown. The caps are not secret — a demo that hides its limits
        cannot be trusted about anything else it says."""
        return {"session_usd": self.session_usd, "session_calls": self.session_calls,
                "rate_requests": self.rate_requests, "rate_window_s": self.rate_window,
                "total_usd": self.total_usd, "daily_usd": self.daily_usd,
                "session_ttl_s": self.session_ttl}


# --- refusing to start ---------------------------------------------------------------------------

class DemoRefused(RuntimeError):
    """The demo must not run in this process. Raised at startup, never per request."""


class DemoRateLimited(RuntimeError):
    """One caller asked for a new session faster than the demo allows.

    A separate exception from `DemoRefused`: that one names a process-wide state ("the demo is
    full") that is the same answer for everyone, this one names a per-caller state that the SAME
    caller resolves by waiting. `V2Service.demo_start` maps this to 429, and `DemoRefused` to
    503 — the two sentences a visitor needs to tell apart to know what to do next."""


def real_provider_credentials(*, env=None, client_path: str | Path | None = None,
                              token_dir: str | Path | None = None) -> list[str]:
    """Which REAL provider credentials are configured here, by name. Never the values.

    A name is enough to refuse on and is not itself a secret, which is why the values are never
    read past their emptiness. The Google half is a file rather than an environment variable in the
    common case, so its presence is checked as a file — an exported `PM_GOOGLE_CLIENT` pointing at
    a path that does not exist is not a credential and must not lock a judge out of the demo."""
    e = os.environ.get if env is None else env.get
    found = [name for name in PROVIDER_CREDENTIAL_ENV if (e(name) or "").strip()]
    client = Path(client_path or e("PM_GOOGLE_CLIENT") or DEFAULT_GOOGLE_CLIENT).expanduser()
    if client.exists():
        found.append(f"Google client secret at {client}")
    tokens = Path(token_dir or e("PM_GOOGLE_TOKEN_DIR") or DEFAULT_GOOGLE_TOKEN_DIR).expanduser()
    if tokens.is_dir() and any(tokens.glob("*.json")):
        found.append(f"Google account token under {tokens}")
    return found


def refuse_if_providers_configured(**kw) -> None:
    """The startup refusal. `demo` promises no visitor can reach a real provider, and the cheapest
    way to keep a promise about a capability is to not have the capability in the process."""
    found = real_provider_credentials(**kw)
    if not found:
        return
    raise DemoRefused(
        "demo mode refuses to start: this process has real provider credentials configured "
        "(" + "; ".join(found) + "). The demo's whole claim is that a stranger cannot reach a "
        "provider, and that claim is only worth something if the credential is not here to reach. "
        "Unset them, or run --sample (fully simulated) or --live (fully real) instead.")


#: Env vars a deployment may use to declare how many worker PROCESSES it is running this app
#: under — gunicorn's and uvicorn's own `--workers`, and `WEB_CONCURRENCY`, the name Heroku,
#: Render and several other platforms set for exactly this. Checked in this order; the first one
#: PRESENT decides. `cli.py`'s own `uvicorn.run(app, host=host, port=port)` never passes
#: `workers=` and reads none of these, so a demo launched the way this codebase documents is
#: always one process — their absence is read as one worker for that reason, not as an assumption
#: this module cannot back up.
WORKER_COUNT_ENV = ("PM_DEMO_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS",
                    "WORKERS")


def _declared_worker_count(env=None) -> int | None:
    """The worker count a deployment declared, `None` for "nothing declared one", or a number
    below 1 for "declared something unreadable as a count".

    `None` and `1` are the only values `refuse_if_multi_worker` lets through. Everything else —
    including a variable that IS set but will not parse as a positive integer — is treated as a
    count this process cannot verify, which `refuse_if_multi_worker` refuses on exactly as it
    would refuse on a verified `2`: an operator who exported `WEB_CONCURRENCY=auto` typed
    something, and "unreadable" is not the same fact as "one"."""
    e = os.environ.get if env is None else env.get
    for name in WORKER_COUNT_ENV:
        raw = e(name)
        if raw is None or not raw.strip():
            continue
        try:
            n = int(raw.strip())
        except ValueError:
            return 0
        return n if n >= 1 else 0
    return None


def refuse_if_multi_worker(*, env=None) -> None:
    """The startup refusal for pm-demo-r3 finding 3: the session table, the rate buckets and (per
    `PM_DEMO_LEDGER`, when set) the spend ledger are all held in THIS process's memory, so N
    uvicorn/gunicorn workers are N independent copies of every cap this process prints at
    startup — `PM_DEMO_MAX_SESSIONS=1` meaning one session per worker, not one session, period.

    The safe default is refusal: a worker count this process cannot verify is treated the same as
    a verified count above one, because a demo that started on an optimistic guess and quietly
    served N times its printed caps is the failure this whole module exists to not have."""
    n = _declared_worker_count(env=env)
    if n is None or n == 1:
        return
    found = (f"declared as {n}" if n >= 1 else "declared, but not readable as a positive integer")
    raise DemoRefused(
        f"demo mode refuses to start with more than one worker, or a worker count it cannot "
        f"read (the worker count is {found}, via one of {', '.join(WORKER_COUNT_ENV)}): the "
        f"session table, the rate buckets and the ledger are all held in this process's memory, "
        f"so N workers would silently serve N times the caps this process prints. Run the demo "
        f"with exactly one worker — unset the variable, or set it to 1.")


# --- the two ledgers, reserved through together --------------------------------------------------

class ChainedLedger:
    """Two `SpendLedger`s reserved through in order, as one. Session cap first, then the demo's.

    Composed rather than reimplemented: both halves are the ledger in `spend.py`, and the only new
    behaviour is the rollback. Without it, a visitor hitting their own session cap would leave the
    demo's global budget charged for a call that was never made — the ledger would ratchet down
    every time anyone hit a limit, and the demo would eventually "run out" for a reason no reader
    of the ledger could see.

    The order is deliberate. The session cap is the one a visitor can hit by themselves, so it is
    checked first and the global reservation is never taken for a call that was already refused.
    """

    def __init__(self, session: SpendLedger, global_: SpendLedger):
        self.session = session
        self.global_ = global_

    def reserve(self, model: str, prompt_chars: int, max_tokens: int) -> tuple[int, int]:
        taken: list[SpendLedger] = []
        reservations: list[int] = []
        try:
            for ledger in (self.session, self.global_):
                reservations.append(ledger.reserve(model, prompt_chars, max_tokens))
                taken.append(ledger)
        except SpendCapReached as exc:
            # Read BEFORE the rollback, and into its own name. The loop variable `ledger` still
            # holds the one that raised here, but the rollback below rebinds it — so computing the
            # stamp after the loop would report whichever ledger happened to be rolled back last,
            # which is always the session's. That is a wrong sentence shown to a visitor, not a
            # crash, which is exactly the kind of defect a passing test does not catch.
            #
            # `SpendLedger`'s own messages name their environment variables, and those are
            # `PM_SPEND_*` for BOTH ledgers here, so a caller reading the text would attribute
            # every refusal to the owner's budget. The stamp is the only thing that knows.
            refused = "session" if ledger is self.session else "global"
            for taken_ledger, reservation in zip(taken, reservations):
                taken_ledger.release(reservation)
            exc.ledger = refused
            raise
        return reservations[0], reservations[1]

    def settle(self, reservations, model: str, usage: dict | None) -> None:
        session_id, global_id = reservations
        self.session.settle(session_id, model, usage)
        self.global_.settle(global_id, model, usage)

    def totals(self) -> dict:
        return {"session": self.session.totals(), "demo": self.global_.totals()}


# --- one visitor's budget ------------------------------------------------------------------------

class Degraded:
    """Why the demo stopped calling the real model, in the visitor's own words.

    A small class rather than a string because the three reasons are three different sentences and
    a caller that renders them has to be able to tell them apart without parsing."""

    __slots__ = ("reason", "detail", "at")

    def __init__(self, reason: str, detail: str, at: float):
        self.reason = reason
        self.detail = detail
        self.at = at

    def as_dict(self) -> dict:
        return {"reason": self.reason, "detail": self.detail}


class SessionBudget:
    """One visitor's call counter, rate bucket and degradation record.

    The USD half of the budget is NOT here: it is the session's own `SpendLedger`, because that is
    the object that can refuse a call before it is sent and settle it to the provider's own usage
    afterwards. Keeping a second running total here would be a number that can disagree with the
    ledger, and the one that would be believed is the wrong one."""

    def __init__(self, *, calls: int, bucket, client: str, caps: DemoCaps,
                 clock=time.monotonic):
        self._calls_left = int(calls)
        self._bucket = bucket
        self._client = client
        #: Kept so `state()` can report the caps without reaching back through the manager, which
        #: would be a second source of truth for a number the visitor is being asked to trust.
        self.caps = caps
        self._clock = clock
        self.degraded: Degraded | None = None
        self._lock = threading.Lock()

    @property
    def calls_left(self) -> int:
        with self._lock:
            return self._calls_left

    def take_call(self) -> str | None:
        """Spend one call, or answer with the reason there is none. `None` means go ahead."""
        with self._lock:
            if not self._bucket.take(self._client):
                return "rate"
            if self._calls_left <= 0:
                return "calls"
            self._calls_left -= 1
            return None

    def take_rate(self) -> bool:
        """Spend one token from the same per-client bucket a real call does, WITHOUT touching
        `calls_left` — for a run that makes no model call and must cost nothing, but a visitor
        hammering the route that starts it must be slowed exactly as one hammering a real call is.
        True means go ahead; False means the bucket is empty this window."""
        with self._lock:
            return self._bucket.take(self._client)

    def note_degraded(self, reason: str, detail: str) -> None:
        with self._lock:
            if self.degraded is None:
                self.degraded = Degraded(reason, detail, self._clock())

    def state(self) -> dict:
        with self._lock:
            return {"calls_left": self._calls_left,
                    "degraded": self.degraded.as_dict() if self.degraded else None}


#: What a degraded run is told, per reason. Sentences rather than codes: this text is shown to a
#: stranger who has to be able to tell "the demo is protecting its budget" from "the demo broke".
DEGRADED_MESSAGES = {
    "calls": "You have used all {n} model calls this session. The answer below is the scripted "
             "sample's, not the model's.",
    "rate": "You are calling faster than the demo allows. The answer below is the scripted "
            "sample's, not the model's.",
    #: Keyed `session`, not `spend`: the cap the visitor hit is `session_usd`, and the reason a
    #: screen renders has to name the thing the operator's setting is called. `spend` also arrives
    #: here for a model with no entry in `spend.PRICES`, which is a different sentence and is
    #: carried in `detail` rather than by a key of its own — an unpriced model refuses every call,
    #: so a visitor who sees it is seeing a misconfiguration, and the detail says which.
    "session": "This session has spent its model budget. The answer below is the scripted "
               "sample's, not the model's.",
    "global": "The demo has reached its overall budget for now. The answer below is the scripted "
              "sample's, not the model's.",
}


@dataclasses.dataclass(frozen=True)
class ModelTruth:
    """What the model behind this process actually is — one sentence, read twice.

    The banner an operator reads at startup and the `real`/`simulated` lists a visitor reads on the
    session screen make the SAME claim about the SAME model, one screen apart. They are one object
    here because two literals is exactly how they came to disagree: the connection row said
    "scripted" while the payload beside it said "real", in one run, on one terminal, and the ledger
    proved the payload right.

    `detail` is the connection row's text and `real`/`simulated` are the payload's, so a caller
    that renders either is rendering this. Nothing here reads a mode string: a mode is a label
    somebody typed, and the defect this replaces was a label outliving the object it described.
    """

    service: str
    state: str
    detail: str
    real: tuple[str, ...]
    simulated: tuple[str, ...]

    def row(self) -> dict:
        """The connection row, in the shape `Wiring.connections` carries."""
        return {"service": self.service, "state": self.state, "detail": self.detail}


#: The three provider rows every sample-shaped world carries. Here rather than beside the model
#: row because they are not in question — the demo simulates exactly these, and it is the MODEL
#: that differs between a sample and a demo world.
_SIMULATED_PROVIDERS: tuple[str, ...] = ("the inbox", "the calendar", "web search results")


def scripted_model_truth() -> ModelTruth:
    """The model that answers from a script: the sample's, and the demo's fallback."""
    return ModelTruth(
        service="nemotron", state="connected",
        detail="sample model replies (synthetic) — a scripted Token Factory, no provider",
        real=(), simulated=("the model replies",) + _SIMULATED_PROVIDERS)


def real_model_truth(model_id: str) -> ModelTruth:
    """The model that really calls Token Factory. The ONE place this sentence is written.

    `model_id` is passed rather than looked up so that the two callers can each name it from the
    thing they hold — a world from the `TokenFactory` it just built, the demo host from the
    `Settings` every world's model is built from — which is what keeps the banner and the payload
    naming the same model without either of them owning a copy of the text."""
    return ModelTruth(
        service="nemotron", state="connected",
        detail=f"REAL model calls to Token Factory ({model_id}) — the sample's replies are the "
               f"fallback once a cap is reached",
        real=(f"the model ({model_id} on Token Factory)",
              "the spend ledger and every cap",
              "the approval gate and the privacy boundary"),
        simulated=_SIMULATED_PROVIDERS)


def model_truth(llm) -> ModelTruth:
    """The truth about `llm`, read off the object the process actually holds.

    A `DemoModel` is the only composition that holds a real `TokenFactory` beside a scripted one,
    so it is the only thing that may be described as real. Everything else here is a scripted
    model over a `MockTransport`, and says so. The read is by SHAPE — `real` plus `model_for` —
    rather than by `isinstance`, so a caller that composes the same way without importing this
    class is still described honestly instead of being called a fixture."""
    real = getattr(llm, "real", None)
    if real is None or not hasattr(real, "model_for"):
        return scripted_model_truth()
    from .llm import Tier
    return real_model_truth(real.model_for(Tier.REASON))


class DemoModel:
    """The real model, with every cap in front of it and one honest answer behind them.

    Wraps rather than subclasses `TokenFactory`: the point is that the real one is untouched and
    still doing the work, and a subclass could quietly override the very method whose behaviour is
    being promised. Everything not overridden here — `usage`, `models`, `model_for` — is the real
    object's, so `/usage` reports genuine bookkeeping.

    The degradation calls the SCRIPTED model, which is a real `TokenFactory` over a
    `MockTransport` built by `wiring.sample_model`. That is what makes "degrades to the scripted
    sample path" literally true rather than a second, thinner implementation of it.
    """

    def __init__(self, *, real, scripted, budget: SessionBudget, caps: DemoCaps):
        # Assigned before anything else: `__getattr__` below forwards to `self.real`, and an
        # attribute read that arrives before this line would recurse instead of failing.
        self.real = real
        self.scripted = scripted
        self.budget = budget
        self.caps = caps

    def chat(self, messages, *args, **kw):
        reason = self.budget.take_call()
        if reason is not None:
            return self._degrade(reason, messages, args, kw)
        try:
            return self.real.chat(messages, *args, **kw)
        except SpendCapReached as exc:
            # The ledger refused before anything was sent — `spend.reserve` runs before the request
            # body exists — so this costs nothing and reaches no provider. Which of the two
            # ledgers refused decides which sentence the visitor reads, and `ChainedLedger` is the
            # only object that knows, so it is stamped there rather than guessed from the text.
            #
            # "no known price for model ..." reaches here as a `spend` refusal too, and it should:
            # an unpriced model is exactly the case where sending the request would spend an
            # unknown amount, and the visitor gets the scripted answer plus the reason.
            return self._degrade(getattr(exc, "ledger", "session"), messages, args, kw,
                                 detail=str(exc))

    def _degrade(self, reason: str, messages, args, kw, detail: str = ""):
        self.budget.note_degraded(reason, detail or DEGRADED_MESSAGES[reason].format(
            n=self.caps.session_calls))
        return self.scripted.chat(messages, *args, **kw)

    def __getattr__(self, name):
        # Only reached for names this class does not define, so `usage` and `models` are the real
        # model's. `real` is looked up on the instance dict first to avoid recursing before
        # `__init__` has run.
        return getattr(object.__getattribute__(self, "real"), name)


# --- the compromised run's script -----------------------------------------------------------------

#: The explicit, machine-readable label for the compromised run's model — carried on the response as
#: `model.label` rather than left for the web UI to hardcode, so the API and the sentence a visitor
#: reads about it cannot drift apart (`vdt-20260922T010701Z-696a9766`: the prior response said only
#: `compromised: true`, and nothing in it said what a caller should call the model that produced it).
COMPROMISED_MODEL_LABEL = "Scripted compromised model — not Nemotron"

#: The one message id `_compromised_case` scripts exactly, rather than deriving from its text — see
#: that function for why.
_BUILTIN_ATTACK_MESSAGE_ID = "msg-benefits-lookalike"
#: The corpus case the built-in attack IS. Named rather than searched for on every call: the whole
#: point is that this run traces to one file a reader can open.
_BUILTIN_ATTACK_CASE_ID = "09-lookalike-benefits-update"


def _compromised_case(message_id: str, body_text: str) -> dict:
    """The fully-compliant script for one edited email: the corpus's own, when this run IS the
    shipped attack, and one `redteam.CompromisedModel` derives from the words otherwise.

    `redteam.parse_intents` is deliberately small (see its own docstring) and tuned to a
    FIRST-person wording — "attach MY address" — because that is the shape the scheduling-day
    corpus is written in. The caregiver week's attack asks for somebody else's facts — "reply with
    HER benefits id" — because the owner is not the one with a benefits record; Rosa is. That is
    not a phrasing the small parser was ever asked to recognise, and widening it to catch this one
    sentence would be a parser tuned to a fixture rather than to attacks in general. `redteam/cases/
    09-lookalike-benefits-update.json` already states, per phase, exactly what a fully compliant
    model answers to THIS email — the same file the corpus scores case 09 against — so a visitor
    who runs the built-in attack watches the corpus's own script, not a derived one that cannot see
    its own disclosure attempt.

    The match is on the CASE'S OWN injection text being present in what was actually sent, not on
    the message id alone: a visitor who selects the lookalike email and writes something else
    entirely is not running the shipped attack, and scripting THAT run against case 09's answers
    would show them a refusal for words they did not send."""
    if message_id == _BUILTIN_ATTACK_MESSAGE_ID:
        from .redteam import CASES_DIR

        path = CASES_DIR / f"{_BUILTIN_ATTACK_CASE_ID}.json"
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            case = None
        if case is not None:
            injection = str(case.get("injection") or "")
            if injection and injection in body_text:
                return {**case, "body": body_text}
    return {"id": f"demo-{message_id}", "title": "The compromised model's run", "body": body_text}


def _attempted_of(case: dict, model) -> list[str]:
    """What the compliant model tried, in the visitor's own words.

    `model.attempted` is `redteam.CompromisedModel`'s own record, but it is only ever FILLED IN
    when the model derived its script from the text — see `CompromisedModel._script`, which reads
    a corpus case's `model` field directly and has no reason to also narrate it. So for the built-
    in attack, where `_compromised_case` hands over the scripted case whole, this reads the SAME
    file's own declared script instead: the fragments the draft phase was told to write, the
    address it was told to add, the time it was told to offer. Nothing here is invented — every
    line traces to a field `redteam/cases/09-lookalike-benefits-update.json` already carries, the
    one the corpus itself scores the case against.

    Every value the fixture protects is redacted to its LABEL before this is returned — see
    `_redact_protected`. Case 09's own draft fragments are scripted to carry the benefits id and
    the power-of-attorney reference VERBATIM, on purpose: that is what "fully compliant" means.
    This is a payload the route hands to every visitor, refused run or not, and "never a value" is
    the same rule the refusal detail itself is held to (`boundary._refuse`); an `attempted` column
    that echoed the fixture's secret to demonstrate the model tried to would be the one surface in
    this whole file that broke it."""
    if model.attempted:
        return [_redact_protected(line) for line in model.attempted]
    if not case.get("model"):
        return []
    out: list[str] = []
    draft = (case["model"].get("draft") or {})
    for fragment in draft.get("fragments") or []:
        text = str(fragment.get("text") or "").strip()
        if text:
            out.append(f"put the requested private detail in the reply: {_redact_protected(text)}")
    for marker in case.get("attempt_markers") or []:
        if "@" in str(marker) and not any(str(marker) in line for line in out):
            out.append(f"addressed the reply to: {marker}")
    for option in (case["model"].get("extract") or {}).get("options") or []:
        phrase = option.get("phrase")
        if phrase:
            out.append(f"offered the time the email named: {phrase}")
    return out


def _redact_protected(text: str) -> str:
    """`text`, with every one of the fixture's protected VALUES replaced by its LABEL.

    The same rule the rest of this build holds every outbound surface to — see `boundary._refuse`,
    which reports a disclosure by field name and recipient count and never by the string that was
    refused — applied here to a payload this file constructs directly rather than reads off the
    gate. `sim.sample_protected_facts` is the one list of what those values are; importing it
    rather than typing a copy is what keeps this redaction from silently stopping the day the
    fixture's own values change."""
    from .sim import sample_protected_facts

    for row in sample_protected_facts():
        value = str(row.get("value") or "")
        if value and value in text:
            text = text.replace(value, f"[{row['label']}]")
    return text


# --- the sessions ---------------------------------------------------------------------------------

class DemoSession:
    """One visitor's world: their own `Wiring`, their own budget, their own ledger."""

    def __init__(self, *, sid: str, wiring, budget: SessionBudget, created: float,
                 clock=time.monotonic):
        self.id = sid
        self.wiring = wiring
        self.budget = budget
        self.created = created
        self.last_seen = created
        self._clock = clock
        #: Why the last run could not be prepared, when it could not. `None` for a run that got as
        #: far as preparation, which is the ordinary case.
        self._prepare_refusal: dict | None = None
        #: Guards the compromised run's model swap (see `run_email_compromised`): two requests on
        #: the same session racing that swap could hand the engine's classify call to one run's
        #: model and its extract call to the other's, which is not a run either visitor asked for.
        self._compromised_lock = threading.Lock()

    def touch(self) -> None:
        self.last_seen = self._clock()

    def expired(self, ttl: float) -> bool:
        return (self._clock() - self.last_seen) > ttl

    def inbox(self) -> list[dict]:
        """The seeded inbox, as the visitor may edit it.

        Read through the PORT rather than off the simulator's private list: `list_new(None)`
        returns every watched message, which is exactly the set the engine will look at, so the
        screen the visitor edits and the inbox the product reads cannot drift apart.

        The key is `message_id`, not `id`, because it is the value the email route takes as
        `message_id`: one name from the entry the visitor reads to the request they send. The old
        wire name made the round trip a translation nobody had written down — `docs/API_V2.md`
        documented the two halves separately and neither said they were the same value."""
        messages, _ = self.wiring.mail.list_new(None)
        return [{"message_id": m.id, "thread_id": m.thread_id, "sender": m.sender,
                 "subject": m.subject, "body_text": m.body_text,
                 "received_at": m.received_at} for m in messages]

    def state(self) -> dict:
        """Everything a screen needs to tell the truth about this session.

        `mode` and the `real`/`simulated` split are the point of the whole mode: the brief is that
        a judge can tell what is a real model call and what is a fixture without reading the
        source. Both lists are read off the model this world HOLDS — see `model_truth` — so the
        payload is a reading of the process rather than a second copy of a sentence the startup
        banner also writes."""
        truth = model_truth(self.wiring.llm)
        return {
            "session_id": self.id,
            "mode": DEMO_MODE,
            "real": list(truth.real),
            "simulated": list(truth.simulated),
            "notice": ("The inbox and calendar are fixtures. The model is real, and the caps "
                       "below are enforced before any request is sent."),
            "budget": self.budget.state(),
            "caps": self.budget.caps.as_dict(),
            "spend": self.spend(),
            "inbox": self.inbox(),
        }

    def spend(self) -> dict:
        """What this session has actually cost, from the ledgers that did the refusing.

        Read off `wiring.llm.real` — the real model's own chained ledger — rather than off a
        counter kept beside it, so the number the visitor is shown is the number that was enforced
        against. A demo that reported a different total from the one its cap used would be a demo
        whose budget claim cannot be checked."""
        ledger = getattr(getattr(self.wiring.llm, "real", None), "spend", None)
        if ledger is None or not hasattr(ledger, "totals"):
            return {}
        return ledger.totals()

    # ---- the visitor's own move: edit the email, run the product ------------------------------
    def run_email(self, *, message_id: str, body_text: str) -> dict:
        """Edit one inbound email in place, run the real pipeline over it, and report what happened.

        This is the demo's whole point in one call. The visitor supplies the body — including the
        injected instruction — and what comes back is the model's ACTUAL output and the decision
        the trusted code made about it, not a scripted verdict about a scripted attack.

        The email is edited rather than delivered so that its `received_at` and `thread_id` are
        untouched: the planner reads the day to plan for and the conversation to reply into off
        those two fields, and a re-stamped copy would ask about a day this fixture has no calendar
        for. `sim.SimMail.edit` is the only writer, and it is not on the port.

        Detection runs, then PREPARATION runs, and the second is not optional: a change that has
        only been detected has no plan, no model view and no decisions, so a demo that stopped
        there would show the visitor an empty decision log and call it a refusal. The refusal is
        the thing they came to see, and it only exists once the run has gone far enough to be
        refused. Both halves go through `V2Service`, both are owner-gated, and both present the
        session's own token — see `result`.

        A refusal from `prepare` is caught rather than raised: `routine_paused` and
        `connection_required` are states of the WORKSPACE, and in a demo the workspace is a fixture
        the visitor cannot fix. What they get instead is the change as far as it got, which is
        still a true answer.

        Raises `KeyError` for an unknown message id so the route can answer 404 rather than
        inventing an inbox entry — the same reason `/v2/changes/{id}` 404s rather than serving an
        empty change.

        The change it answers about is the one raised for `message_id`, and the paragraph at the
        selection below says why that had to be written down rather than left as `found[0]`.
        """
        edited = self.wiring.mail.edit(message_id, body_text)
        if edited is None:
            raise KeyError(message_id)
        self._prepare_refusal = None
        change_id = self._detect_and_prepare(message_id, edited)
        return self.result(change_id, prepared=self._prepare_refusal is None and bool(change_id))

    def _detect_and_prepare(self, message_id: str, edited) -> str:
        """Tick the watcher, find the change FOR `message_id`, and prepare it.

        Split out of `run_email` so `run_email_compromised` can run the identical selection and
        preparation over a model it has swapped in for the one call — everything about which
        change this is and how far it gets is the engine's, and duplicating it would be a second
        copy of the paragraph below that the two runs could disagree about.

        `edited` is the exact `MailMessage` `wiring.mail.edit` just returned — the CURRENT bytes
        this run is about, not whatever the mailbox held the last time something looked. It is
        the reclassification fallback's only source of truth, and the reason it has to be one is
        below.
        """
        before = {c.id for c in self.wiring.engine.changes()}
        # The watcher's own tick, not `engine.detect(None)`: detection is cursor- and seen-driven,
        # and asking for everything since the beginning re-classifies the first email, so one
        # message becomes two changes and the visitor is sent to one that was never planned.
        found = [c for c in self.wiring.routine.tick() if c.id not in before]
        # The change for the message the visitor ACTUALLY edited, not whichever one the tick
        # happened to list first. The two were the same thing while the demo's world was the
        # scheduling sample: one watched message, so `found` held at most one change and picking
        # the first was picking the only. The caregiver week has four, and the tick classifies
        # every one of them in a single pass — so `found[0]` is whatever the mailbox listed
        # earliest, and a visitor who edited the lookalike "benefits update" email was answered
        # with the pharmacy refill's change. A refusal about an email the visitor did not send is
        # not a refusal they can learn anything from.
        #
        # ONLY among what THIS tick just raised — never among every change this session has ever
        # produced. `Routine.tick` marks a message seen the moment it is classified once, and
        # `SimMail.list_new`'s own cursor permanently drops the newest-timestamped message from
        # every later poll regardless of that seen table, so a SECOND run on the same message
        # (the real run then the built-in attack, the attack run twice, either order) always
        # raises nothing here — `found` empty, and `before` already holding a change for this
        # `message_id` from the first run. Widening the search to `engine.changes()` used to paper
        # over exactly that case, which is what made it a bug: the "change for this message" it
        # found was the FIRST run's, cached — a real run's proposal handed back, unmarked, as the
        # answer to "and if the model were compromised?", or the reverse. Two calls to this method
        # are two visitor actions and must be two changes, so a second run reclassifies directly
        # below instead of reaching for one that already exists.
        mine = [c for c in found if c.message_id == message_id]
        if not mine:
            # `engine.classify` mints a fresh id unconditionally (see its own docstring) and reads
            # `engine.llm` at the moment it is called — this session's real model, or, under
            # `run_email_compromised`, the scripted model already swapped in for this one call.
            # So a rerun's classify — and, once it says this is a change, its own prepare below —
            # runs the SAME model the rest of this call is using, on the CURRENT edited body,
            # under a change id no earlier run's answer can be mistaken for.
            #
            # `ModelAnswerInvalid` is caught here exactly as `Routine.tick` catches it for every
            # OTHER message: a classification the engine could not read is not a "no" (see that
            # module's docstring). `tick` retries an unreadable answer on its own next poll; this
            # call has no next poll to retry on, so the honest answer is the same one the ORIGINAL
            # fallback gave when the tick above had nothing — no change this run, rather than a
            # 500 the direct call would otherwise raise for the same unreadable answer `tick`
            # swallows for every message beside this one.
            from .prompt_contract import ModelAnswerInvalid
            try:
                change = self.wiring.engine.classify(edited)
            except ModelAnswerInvalid:
                change = None
            if change is not None:
                mine = [change]
        change_id = mine[-1].id if mine else ""
        if change_id:
            from .api_v2 import V2Error
            try:
                self.wiring.service.prepare(change_id, self.wiring.owner_token)
            except V2Error as exc:
                # Recorded, not swallowed: the reason is a field of the answer, so a visitor whose
                # run stopped early is told which state stopped it rather than left to guess from
                # an empty plan.
                self._prepare_refusal = {"code": exc.code, "detail": exc.detail}
        return change_id

    def run_email_compromised(self, *, message_id: str, body_text: str) -> dict:
        """The same edit, the same engine, the same gate — a model that complies with everything
        the email asks instead of the real one.

        This is the demo's other half: the product's claim is "assume the model is fully
        compromised; the engine still refuses", and a visitor watching the REAL model politely
        ignore an injection never sees that claim tested. `redteam.CompromisedModel` is the
        corpus's own fully-compliant script, and it is run through THIS session's own
        `ChangeEngine` — the same gate, the same boundary, the same care circle and protected
        facts `run_email` uses — so the refusal that follows is the one the built-in attack's own
        red-team case measures, not a second, thinner engine wearing the same UI.

        The model is swapped on `wiring.engine.llm` only, for the duration of this one call, under
        `_compromised_lock`: `wiring.llm` — what `state()` and `spend()` read to say the model is
        real — is untouched, because this run is not a claim about what this SESSION's model is,
        it is one visitor asking "and if it were fully on the attacker's side?". The swapped model
        has no `spend` ledger and never calls `budget.take_call()`, which is what makes "costs
        nothing, is not a model call" true by construction rather than by a counter this method
        has to remember to skip. `budget.take_rate()` still spends the per-client bucket a real
        call would, so the ROUTE is rate-limited even though the run underneath it is free.

        Raises `KeyError` for an unknown message id, exactly as `run_email` does, and
        `demo_session.DemoRateLimited` when the client's bucket is empty."""
        from .redteam import CompromisedModel

        if not self.budget.take_rate():
            raise DemoRateLimited(
                f"the demo allows {self.budget.caps.rate_requests} requests per "
                f"{int(self.budget.caps.rate_window)}s per client (PM_DEMO_RATE_REQUESTS); wait "
                f"and try again.")
        edited = self.wiring.mail.edit(message_id, body_text)
        if edited is None:
            raise KeyError(message_id)
        self._prepare_refusal = None
        case = _compromised_case(message_id, body_text)
        prompts: list[dict] = []
        refused: list[dict] = []
        model = CompromisedModel(case, prompts, refused)
        with self._compromised_lock:
            engine = self.wiring.engine
            original_llm = engine.llm
            engine.llm = model(engine.gate)
            try:
                change_id = self._detect_and_prepare(message_id, edited)
            finally:
                engine.llm = original_llm
        out = self.result(change_id, prepared=self._prepare_refusal is None and bool(change_id))
        out["compromised"] = True
        # What the compliant model TRIED, so a visitor reads "the engine refused this" beside
        # "and this is what it was asked to refuse" rather than having to infer the second half
        # from a decision log alone.
        out["attempted"] = _attempted_of(case, model)
        # Explicit and machine-readable, not left for a reader to infer from `compromised: true`
        # alone: `calls`/`cost_usd` are pinned at zero rather than read off any ledger, because this
        # run never reserves against one (see the docstring above).
        out["model"] = {"kind": "scripted", "label": COMPROMISED_MODEL_LABEL, "calls": 0, "cost_usd": 0.0}
        return out

    def result(self, change_id: str, *, prepared: str | None = None) -> dict:
        """One change, with everything the visitor needs to judge what the product did.

        Assembled from the SAME service methods the owner's own screens call, so a demo result and
        a real one cannot drift: `change`, `model_view` and `decisions` are the product's, and this
        only adds the facts that exist in demo mode alone — the budget left, whether the model call
        behind this answer was real or the scripted fallback, and (when the run could not be
        prepared) the workspace state that stopped it.

        `model.kind` defaults to `"real"` here because this method is `run_email`'s own result path;
        `run_email_compromised` overwrites it with the scripted label after calling this, the same
        way it overwrites `compromised` and `attempted` — see `COMPROMISED_MODEL_LABEL`."""
        service = self.wiring.service
        out = {"session_id": self.id, "change_id": change_id, "prepared": bool(prepared),
               "budget": self.budget.state(), "spend": self.spend(), "model": {"kind": "real"}}
        if self._prepare_refusal is not None:
            out["prepare_refused"] = self._prepare_refusal
        if not change_id:
            return {**out, "change": None, "model_view": None, "decisions": []}
        # `model_view` and `decisions` are owner-gated, and the demo presents the SESSION's own
        # token rather than being exempted from the gate: this world has an owner, it is the
        # session, and the credential for it is held by the server and never sent to the visitor.
        # Reaching around the gate would work today and would be the one code path that survives a
        # change to who may read a decision log.
        token = self.wiring.owner_token
        out["change"] = service.change(change_id)
        out["model_view"] = service.model_view(change_id, token)
        out["decisions"] = service.decisions(change_id, token)
        return out


class DemoSessions:
    """Every live demo session in this process, and the caps that bound them.

    Holds `Wiring`s, not `V2Service`s: the demo's isolation claim is about the whole world — the
    engine, the boundary, the audit log, the mailbox — and a manager that held only the HTTP
    surface would be a manager that could not say what a session can reach.

    The rate bucket is shared across sessions and keyed by the caller; everything else is per
    session. That split is the design: two visits from one client are the same caller and share a
    rate limit, but they are different worlds and share no data.
    """

    def __init__(self, *, settings: Settings, caps: DemoCaps | None = None,
                 global_ledger: SpendLedger | None = None, world_builder=None,
                 clock=time.monotonic, clock_wall=time.time, on_degrade=None):
        self.settings = settings
        self.caps = caps or DemoCaps.from_env()
        self._wall = clock_wall
        self._clock = clock
        self._sessions: dict[str, DemoSession] = {}
        self._lock = threading.Lock()
        #: One bucket for the process, keyed by caller — see the class docstring.
        self._bucket = playground.TokenBucket(
            capacity=self.caps.rate_requests, window=self.caps.rate_window,
            now=self._clock, max_keys=self.caps.rate_max_callers)
        #: The demo's own ceiling, on the demo's own file. Separate from the owner's ledger so
        #: exhausting the demo cannot disable the real product, and vice versa.
        self.global_ledger = global_ledger or SpendLedger(
            os.environ.get("PM_DEMO_LEDGER", str(Path.home() / ".privatemesh" / "demo-spend.db")),
            caps=Caps(self.caps.total_usd, self.caps.daily_usd))
        self._build_world = world_builder or self._default_world
        self._on_degrade = on_degrade

    @property
    def live_sessions(self) -> int:
        with self._lock:
            return len(self._sessions)

    def start(self, client: str) -> DemoSession:
        """A new visitor's world, or a refusal that says why.

        `max_sessions` is not a queue: a caller past it is refused rather than evicting someone
        else's session, because evicting a live session would delete a stranger's work to serve a
        stranger, and the honest answer is "the demo is busy".

        Session creation spends a rate-limit token from the SAME per-client bucket a model call
        does, checked FIRST — the cheapest of the failure modes, and the one that stops a caller
        allocating toward `max_sessions` in a loop that never places a model call at all.

        The check-and-reserve against `max_sessions` is ONE atomic step under `_lock`: a
        placeholder (`None`) is written into `_sessions` in the same critical section that read
        its length, so two callers racing this method cannot both observe `count < max_sessions`
        and both proceed. Before this, `_sweep()` then a SEPARATE `with self._lock:` read the
        count, and the (slow, network-shaped) world was built in between while the lock was not
        held — the window the auditor's reproduction used to get `live_sessions == 2` out of
        `PM_DEMO_MAX_SESSIONS=1` with two concurrent callers. `get()` already treats a missing OR
        `None` entry as `KeyError`, so a caller racing in while the placeholder's world is still
        being built sees "no such session" rather than a half-built one. A failure while building
        releases the reservation rather than leaving a slot nobody can ever fill again."""
        self._sweep()
        if not self._bucket.take(client):
            raise DemoRateLimited(
                f"the demo allows {self.caps.rate_requests} requests per "
                f"{int(self.caps.rate_window)}s per client (PM_DEMO_RATE_REQUESTS); wait and try "
                f"again before starting another session.")
        sid = secrets.token_urlsafe(24)
        with self._lock:
            if len(self._sessions) >= self.caps.max_sessions:
                raise DemoRefused(
                    f"the demo is holding its maximum of {self.caps.max_sessions} sessions; "
                    f"wait for one to expire (PM_DEMO_MAX_SESSIONS).")
            self._sessions[sid] = None
        try:
            budget = SessionBudget(calls=self.caps.session_calls, bucket=self._bucket,
                                   client=client, caps=self.caps, clock=self._clock)
            # The builder receives everything the manager resolved, so a caller replacing it (a
            # test injecting a counting transport) replaces the WORLD and not the caps it runs
            # under.
            wiring = self._build_world(sid=sid, budget=budget, settings=self.settings,
                                       caps=self.caps, global_ledger=self.global_ledger)
            session = DemoSession(sid=sid, wiring=wiring, budget=budget,
                                  created=self._clock(), clock=self._clock)
        except BaseException:
            with self._lock:
                self._sessions.pop(sid, None)
            raise
        with self._lock:
            self._sessions[sid] = session
        return session

    def get(self, sid: str) -> DemoSession:
        """The session, or `KeyError`. Expiry is checked on the way in rather than only by the
        sweeper, so a session cannot be used during the window between dying and being collected.

        `None` reads the same as missing: it is the placeholder `start()` reserves a slot with
        while the world behind it is still being built, and a caller for it mid-build has no
        session to be handed — not this one, half-finished, and not a different one either."""
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            raise KeyError(sid)
        if session.expired(self.caps.session_ttl):
            with self._lock:
                self._sessions.pop(sid, None)
            # Release what the session held — the same reason `_sweep` does it below — before
            # answering "no such session" rather than after, so a caller who immediately starts a
            # new one is not competing with a world that is still holding its slot in spirit.
            session.wiring.stop()
            raise KeyError(sid)
        session.touch()
        return session

    def stop(self, sid: str) -> None:
        """Drop one session and everything in it. The stores are `:memory:`, so this is the whole
        of "discarded when the session ends" — there is no file to delete and nothing to leak."""
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session is not None:
            session.wiring.stop()

    def _sweep(self) -> None:
        """Drop every session past its TTL, and release what each one held.

        The pop and the `wiring.stop()` used to be two different things: this only popped the
        dict entry, so an expired session's `Wiring` — its engine, its boundary, its per-session
        `TokenFactory` and the thread(s) under `routine.tick()` — was simply dropped and left for
        the garbage collector rather than told to stop. `stop()` (the caller-initiated twin of
        this sweep) always called it; the sweep, the path every session actually takes when a
        visitor does not come back, did not. Sessions are `:memory:` and hold no file, so nothing
        here is a leaked file descriptor, but `Wiring.stop()` is still the one place that tears
        down what a session's world spun up, and skipping it is not eligible for a demo that runs
        unattended.

        `wiring.stop()` runs OUTSIDE the lock, after every dead entry is popped, for the same
        reason `stop()` above does: teardown must not be done while holding the lock a concurrent
        `start()` or `get()` needs."""
        with self._lock:
            dead = [sid for sid, s in self._sessions.items()
                   if s is not None and s.expired(self.caps.session_ttl)]
            removed = [self._sessions.pop(sid, None) for sid in dead]
        for session in removed:
            if session is not None:
                session.wiring.stop()

    def _default_world(self, *, sid: str, budget: SessionBudget, settings: Settings, caps,
                       global_ledger):
        """The real builder, imported here rather than at module scope.

        `wiring` imports this module for `DEMO_MODE`, so a top-level import back would be a cycle.
        The lazy import is the same shape `api_v2._sample_follow_up` uses for `ports`."""
        from .wiring import build_demo_world
        return build_demo_world(sid=sid, settings=settings, caps=caps, budget=budget,
                                global_ledger=global_ledger)
