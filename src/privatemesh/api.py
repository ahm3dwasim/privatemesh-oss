"""The HTTP surface — one turn per request, and a receipt for everything it did.

The endpoints are deliberately thin: each one is a view onto a store that already exists (memory,
grants, approvals, audit), so there is no second implementation of any rule here. The one that
matters most is `POST /approvals/{id}` — approving a pending action and resuming the turn it
paused — because that is the product's central moment, and it goes through the same single-use
`ApprovalStore.consume` as everything else.

`build_app` takes its collaborators, which is what lets the tests drive the whole API over fakes
with no network and no model.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import demo as demo_mode
from . import demo_session
from . import memory as mem
from .agent import Agent
from .approval import ApprovalError, ApprovalStore
from .audit import AuditError, AuditLog
from .broker import Broker
from .commands import MemoryCommands
from .config import Settings
from .grants import MODES, GrantStore
from .llm import TokenFactory
from .skills import SkillRegistry
from .skills_builtin import build_registry
from .turnstore import TurnStore

log = logging.getLogger(__name__)


class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    turn_id: str | None = None      # resume this paused turn instead of starting a new one


class MemoryIn(BaseModel):
    text: str = Field(min_length=1)
    type: str = "semantic"
    entity: str = ""
    project: str = ""
    importance: int = 3
    approved: bool = False           # consent is opt-in: a new memory starts pending


class GrantIn(BaseModel):
    skill: str = Field(min_length=1)
    mode: str


class DecisionIn(BaseModel):
    decision: str                    # "approve" | "deny"
    turn_id: str | None = None


def build_app(*, agent: Agent | None = None, conn: sqlite3.Connection | None = None,
              audit: AuditLog | None = None, broker: Broker | None = None,
              grants: GrantStore | None = None, llm: TokenFactory | None = None,
              registry: SkillRegistry | None = None,
              settings: Settings | None = None,
              commands: MemoryCommands | None = None,
              v2: "object | None" = None,
              mount_v1: bool = True) -> FastAPI:
    # `mount_v1=False` is the public v2 demo's build: `/health`, `/docs` and the four
    # `/v2/demo/*` routes are the whole surface, and the v1 chat-and-receipts routes below are not
    # merely skipped, they are never registered on `app` at all. The caller (`default_app`) goes
    # further and never builds the v1 collaborators (`agent`, `conn`, `audit`, ...) in that case
    # either — gating a route that was still built is the finding this replaces.
    #
    # Memory mutations go through one command layer rather than touching the store directly, so
    # each change is receipted before it happens (see `commands.py`). Built here by default so
    # existing callers keep working unchanged.
    if mount_v1:
        commands = commands or MemoryCommands(conn, audit)
    app = FastAPI(title="PrivateMesh", version="0.0.1",
                  summary="A personal AI that can prove it only did what you allowed.")

    # The v2 router ("Handle the change") is mounted only when a service is supplied. It is a
    # separate surface on purpose: v1 is the chat-and-receipts product, v2 is one delegation with
    # a versioned approval, and nothing in v2 reaches a provider through a v1 path.
    if v2 is not None:
        from .api_v2 import (assert_every_v2_route_is_gated, assert_v2_gate_is_innermost,
                             build_sample_router, build_v2_router, install_v2_request_gate)
        app.include_router(build_v2_router(v2))
        # The sample's own demo control, deliberately NOT under `/v2`: `/v2` is the contract the
        # web client is written against, and an endpoint that exists only because the sample has a
        # scripted inbox would be a route every live caller has to be told to ignore. It refuses
        # in live mode, so it cannot become one by accident.
        app.include_router(build_sample_router(v2))
        # The gate is a ROUTE CLASS set as the router's default, which is a property of the router
        # rather than of the app: anything registered on this app by another path — a per-route
        # `route_class_override`, `app.add_api_route`, a raw Starlette `add_route` or a `Mount`, a
        # router mounted later — is not covered by it and would answer anonymously. So the assembled
        # app is examined at startup, EVERY route it serves is enumerated whatever its class and
        # with the path it is actually reachable at, and a `/v2` route that is neither gated nor
        # vetted public stops the process instead of serving.
        app.router.on_startup.append(lambda: assert_every_v2_route_is_gated(app))
        assert_every_v2_route_is_gated(app)
        # And the same question is asked of every request, because that scan is a fact about the
        # route table at ONE moment: `app.add_api_route("/v2/after-startup", ...)` after this line
        # answered 200 anonymously until the eighth review measured it. Installing the middleware is
        # what makes the gate a boundary rather than a boot check, so it is done here, once, for
        # every app this function builds — the CLI's, the demo's and the live harness's alike.
        install_v2_request_gate(app)

    # The web app is a separate origin, so the browser needs a grant to read these responses. It is
    # an explicit list, never "*": this API spends the owner's budget and reads the owner's memories.
    #
    # Added LAST and therefore OUTERMOST (`add_middleware` inserts at the front), which is a decision
    # rather than a detail of where the line ended up. The per-request gate above refuses a `/v2`
    # request with a 403 carrying a `detail` that explains the wiring defect — and with CORS inside
    # that refusal, a browser could not read it: the response arrives with no
    # `access-control-allow-origin`, so the client sees an opaque network failure and the sentence
    # written to be read is never read. The round-2 review's finding was a refusal nobody saw, and a
    # refusal a browser is not allowed to show is the same defect in a different layer. Outermost,
    # CORS also answers preflight before the gate resolves a route for a request no handler serves.
    app.add_middleware(CORSMiddleware, allow_origins=_web_origins(settings),
                       allow_methods=["*"], allow_headers=["*"])

    # The ordering above is load-bearing in both directions and is now a checked invariant rather
    # than a comment about where two lines happened to land: CORS must be OUTSIDE the gate (so its
    # grant is on the refusal) and the gate must be INNERMOST (so nothing can run between its
    # resolution and the dispatch it approved). Checked here, on the app the product actually
    # serves, because that is the one a rewiring would break and the one no test app stands in for.
    if v2 is not None:
        assert_v2_gate_is_innermost(app)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "skills": sorted(registry.names()) if registry is not None else []}

    if not mount_v1:
        return app

    # ---- chat ------------------------------------------------------------------------------
    @app.post("/chat")
    def chat(body: ChatIn) -> dict:
        if body.turn_id:
            try:
                turn = agent.resume(body.turn_id)
            except KeyError:
                raise HTTPException(status_code=404, detail=f"no turn '{body.turn_id}'")
        else:
            turn = agent.run(body.message)
        return turn.as_dict()

    @app.get("/turns/{turn_id}")
    def get_turn(turn_id: str) -> dict:
        turn = agent.turn(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail=f"no turn '{turn_id}'")
        return turn.as_dict()

    @app.get("/turns/{turn_id}/receipts")
    def turn_receipts(turn_id: str) -> dict:
        """The turn's receipts, assembled for the UI: one card per plan step.

        Decides nothing — it joins three things that already exist and are already the truth: the
        step the plan proposed (which carries the exact arguments), the outcome the runner
        recorded, and the `policy.decision` rows the broker wrote to the chain (which carry the
        risk, the decision, the owner's answer, the timestamp and the hash). The hash is the point:
        a receipt without one is just a log line."""
        turn = agent.turn(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail=f"no turn '{turn_id}'")
        return {"turn_id": turn_id, "task_id": turn.task_id, "status": turn.status,
                "receipts": _assemble_receipts(turn, audit)}

    @app.get("/turns/{turn_id}/model_saw")
    def model_saw(turn_id: str) -> dict:
        """Exactly which approved memories left the machine for this turn."""
        turn = agent.turn(turn_id)
        if turn is None:
            raise HTTPException(status_code=404, detail=f"no turn '{turn_id}'")
        events = [r for r in audit.rows(task_id=turn.task_id) if r["event"] == "model_saw"]
        return {"turn_id": turn_id, "task_id": turn.task_id, "memory_ids": turn.memory_ids,
                "memories": turn.memories, "events": events}

    # ---- memories --------------------------------------------------------------------------
    @app.get("/memories")
    def list_memories(limit: int = 50, approved_only: bool = False) -> dict:
        rows = mem.recent_memories(conn, limit=limit, approved_only=approved_only)
        return {"memories": [h.__dict__ for h in rows], "counts": mem.counts_by_type(conn)}

    @app.post("/memories")
    def create_memory(body: MemoryIn) -> dict:
        new_id = commands.write(type=body.type, text=body.text, entity=body.entity,
                                project=body.project, importance=body.importance,
                                approved=body.approved)
        if not new_id:
            raise HTTPException(status_code=400, detail="nothing worth storing (empty or fully redacted)")
        return {"id": new_id, "approved": body.approved}

    @app.post("/memories/{memory_id}/approve")
    def approve_memory(memory_id: int, approved: bool = True) -> dict:
        if not commands.set_consent(memory_id, approved=approved):
            raise HTTPException(status_code=404, detail=f"no memory {memory_id}")
        return {"id": memory_id, "approved": approved}

    @app.post("/memories/{memory_id}/forget")
    def forget_memory(memory_id: int) -> dict:
        hit = mem.get_memory(conn, memory_id)
        if hit is None:
            raise HTTPException(status_code=404, detail=f"no memory {memory_id}")
        changed = commands.forget(memory_id, text=hit.text)
        return {"id": memory_id, "forgotten": changed, "text": hit.text}

    @app.get("/memories/forgotten")
    def list_forgotten(limit: int = 50) -> dict:
        """What it forgot, and when — kept visible on purpose. A memory it can no longer recall is
        still a row the owner can point at."""
        rows = mem.forgotten_memories(conn, limit=limit)
        return {"memories": rows, "count": len(rows)}

    # ---- grants ----------------------------------------------------------------------------
    @app.get("/grants")
    def list_grants() -> dict:
        return {"grants": grants.all(), "modes": list(MODES)}

    @app.post("/grants")
    def set_grant(body: GrantIn) -> dict:
        if body.skill not in registry.names():
            raise HTTPException(status_code=404, detail=f"unknown skill '{body.skill}'")
        try:
            mode = grants.set(body.skill, body.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        audit.record("grant.changed", detail={"skill": body.skill, "mode": mode})
        return {"skill": body.skill, "mode": mode}

    # ---- approvals -------------------------------------------------------------------------
    @app.get("/approvals")
    def list_approvals(status: str | None = None) -> dict:
        return {"approvals": [r.as_dict() for r in broker.approvals.list(status=status)]}

    @app.get("/approvals/recoverable")
    def recoverable_approvals() -> dict:
        """What a restart must not lose: decisions still waiting on the owner, and decisions the
        owner already made that nothing has spent yet.

        Approvals and paused turns are both durable, and each approval names the turn it paused,
        so after a restart the owner can see exactly what is outstanding and resume it. Without
        this, an approve-then-restart would abandon a run the owner believes was handled."""
        groups = broker.approvals.recoverable()
        turns = {}
        for req in groups["pending"] + groups["approved_unconsumed"]:
            if req.turn_id:
                stored = agent.turn(req.turn_id)
                turns[req.turn_id] = {
                    "turn_id": req.turn_id, "status": stored.status if stored else "unknown",
                    "paused_step": stored.paused_step if stored else None,
                    "approval_id": req.id}
        return {"pending": [r.as_dict() for r in groups["pending"]],
                "approved_unconsumed": [r.as_dict() for r in groups["approved_unconsumed"]],
                "turns": list(turns.values())}

    @app.post("/approvals/{approval_id}")
    def decide(approval_id: str, body: DecisionIn) -> dict:
        """Approve or deny a pending action. On approve the paused turn resumes and the action
        runs; the approval is spent exactly once and can never be replayed (409 on a second try)."""
        if body.decision not in ("approve", "deny"):
            raise HTTPException(status_code=400, detail="decision must be 'approve' or 'deny'")
        try:
            if body.decision == "deny":
                inv = broker.decide(approval_id, approve=False)
                turn = agent.resume_for_approval(approval_id)
                return {"approval_id": approval_id, "decision": "deny",
                        "invocation": inv.__dict__, "turn": turn.as_dict() if turn else None}
            # Mark it granted first, so the resume path (which re-checks the store) can see that
            # the owner has actually decided. `consume` is still what makes it single-use.
            broker.approvals.approve(approval_id)
            turn = agent.resume_for_approval(approval_id)
            if turn is not None:
                return {"approval_id": approval_id, "decision": "approve", "invocation": None,
                        "turn": turn.as_dict()}
            # No turn was waiting on it (a direct broker call) — decide and run it here.
            inv = broker.decide(approval_id, approve=True)
            return {"approval_id": approval_id, "decision": "approve",
                    "invocation": inv.__dict__, "turn": None}
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # ---- audit -----------------------------------------------------------------------------
    @app.get("/audit")
    def get_audit(task_id: str | None = None, event: str | None = None, limit: int | None = None) -> dict:
        try:
            return {"rows": audit.rows(task_id=task_id, event=event, limit=limit)}
        except AuditError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/audit/verify")
    def verify_audit() -> dict:
        v = audit.verify()
        return {"ok": v.ok, "broken_id": v.broken_id, "note": v.note}

    # ---- usage -----------------------------------------------------------------------------
    @app.get("/usage")
    def usage() -> dict:
        u = llm.usage
        return {"calls": u.calls, "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens, "by_model": u.by_model,
                "models": {"fast": settings.model_fast if settings else "",
                           "reason": settings.model_reason if settings else "",
                           "deep": settings.model_deep if settings else ""}}

    return app


DEFAULT_WEB_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")


def _detail(row: dict) -> dict:
    """An audit row's `detail` is canonical JSON text. Never raises on a malformed one."""
    try:
        data = json.loads(row.get("detail") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _asked(row: dict) -> bool:
    """True when this row is the broker ASKING and nobody has answered yet.

    `decision` alone cannot tell you that: it is the *policy* verdict, so an owner-approved
    HIGH-risk action is still filed as `needs_approval`. What changes when the owner answers is
    `approved` / `executed` — which is why the caller must not read `decision` as the outcome."""
    d = _detail(row)
    return (d.get("decision") == "needs_approval" and d.get("approved") is None
            and not d.get("executed"))


def _verdict(chain: list[dict]) -> str:
    """The stamp the receipt carries, derived from the whole chain of rows for that step."""
    if not chain:
        return "not_reached"
    d = _detail(chain[-1])
    if _asked(chain[-1]):
        return "pending"
    if d.get("approved") is True:
        return "approved"
    if d.get("approved") is False:
        return "refused"
    if d.get("executed"):
        return "allowed" if d.get("ok") else "failed"
    return "denied" if d.get("decision") == "deny" else "not_reached"


def _groups(rows: list[dict]) -> list[list[dict]]:
    """Split one skill's policy rows into per-attempt groups: the rows where the broker asked, then
    the single row where it settled. A step that paused and was later approved produced
    [asked, settled] — ONE receipt with a history, not two cards."""
    groups, cur = [], []
    for r in rows:
        cur.append(r)
        if not _asked(r):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _assemble_receipts(turn, audit: AuditLog) -> list[dict]:
    """Join plan step ↔ run outcome ↔ audit rows. Pure view: no rule is re-decided here."""
    rows = [r for r in audit.rows(task_id=turn.task_id) if r["event"] == "policy.decision"]
    by_tool: dict[str, list[dict]] = {}
    for r in rows:
        by_tool.setdefault(str(_detail(r).get("tool", "")), []).append(r)
    # Pop per tool, in order: two steps that both call notes_write take the rows in the order the
    # broker wrote them, which is the order the runner ran them.
    queue = {tool: _groups(rs) for tool, rs in by_tool.items()}
    outcomes = {o.index: o for o in (turn.report.outcomes if turn.report else ())}
    out = []
    for step in (turn.plan.steps if turn.plan else ()):
        groups = queue.get(step.tool or "", [])
        chain = groups.pop(0) if groups else []
        last = _detail(chain[-1]) if chain else {}
        outcome = outcomes.get(step.index)
        answered = [d.get("approved") for d in (_detail(r) for r in chain)
                    if d.get("approved") is not None]
        verdict = _verdict(chain)
        if verdict == "pending":
            decided_by = "waiting"        # the card is on the owner's desk, unanswered
        elif answered:
            decided_by = "you"
        else:
            decided_by = "policy" if chain else "—"
        out.append({
            "verdict": verdict,
            "step": step.index + 1,
            "description": step.description,
            "tool": step.tool,
            "arguments": step.arguments,
            "expected_evidence": step.expected_evidence,
            "risk": last.get("risk", ""),
            "tier": last.get("tier", ""),
            # The kernel's own policy verdict, kept verbatim for fidelity. Read `verdict` for what
            # actually happened: for an approved HIGH-risk action this still says "needs_approval",
            # because that is what the table decided and the owner then overrode it.
            "decision": last.get("decision", "not_reached"),
            "reason": last.get("reason", ""),
            "decided_by": decided_by,
            "approved": answered[-1] if answered else None,
            "executed": bool(last.get("executed")),
            "ok": last.get("ok"),
            "duration_ms": last.get("duration_ms"),
            "ts": chain[-1]["ts"] if chain else "",
            "hash": chain[-1]["hash"] if chain else "",
            "approval_id": (outcome.approval_id or None) if outcome else None,
            "summary": (outcome.summary if outcome else ""),
            "verified": bool(outcome.verified) if outcome else False,
        })
    return out


def _web_origins(settings: Settings | None) -> list[str]:
    """The CORS allow-list: `PM_WEB_ORIGINS` when set, the local dev pair otherwise."""
    raw = (settings.web_origins if settings else "") or ""
    return [o.strip() for o in raw.split(",") if o.strip()] or list(DEFAULT_WEB_ORIGINS)


def demo_enabled() -> bool:
    return os.environ.get("PM_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}


def default_app(settings: Settings | None = None, *, demo: bool | None = None,
                v2: "object | None" = None) -> FastAPI:
    """Wire the real thing from the environment: one SQLite file for memory/notes, one for audit.

    Two questions look alike and are not: "is this the scripted world" (the v1 store, model and
    registry `demo_mode` swaps in — the older v1 simulated app, `PM_DEMO=1` or bare `--demo`) and
    "is this a public demo" (a `v2` service in `demo_session.DEMO_MODE`, the one build a stranger
    reaches with no token on a public port). A `v2` demo service answers "yes" to both — its v1
    half, if it had one, would need the scripted store too — but a `v2` SAMPLE service answers
    "yes" to the first alone: `--v2 --sample` is the zero-credential local preview, and its v1
    chat routes are part of that preview, not a public surface, so they stay mounted.

    `demo=True` (or `PM_DEMO=1`) swaps the v1 model and the Tavily transport for scripted ones and
    seeds synthetic memories — into its OWN data directory, so a demo run can never write over a
    real store. Everything below the model is the production kernel.

    `v2` is a fully-built `wiring.Wiring.service`. It is passed in rather than constructed here
    because the v2 product owns its own clock, its own stores and its own mode ("sample" is a
    claim about what the providers ARE), and a host that assembled half of it would be a host that
    could label synthetic data "connected".

    A DEMO `v2` service forces the demo store, whatever `demo` says, and — this is the finding this
    docstring used to be silent about — forces the v1 stack not to exist at all: `build_app` is
    called with `mount_v1=False`, so `/health`, `/docs` and the four `/v2/demo/*` routes are the
    whole surface a public demo process serves. Gating the v1 routes while still building them was
    the defect a blind audit demonstrated (`GET /memories` → 200 with no token, on the shared,
    persistent `data/demo` store); not building them is what makes the claim true rather than
    merely enforced. The mode is a property of the thing being served, so taking it from the
    caller instead was a way to mount the OWNER's `/memories` and `/audit` under an app whose whole
    claim is that a stranger cannot reach them: `default_app(SETTINGS, demo=False, v2=<demo
    service>)` served the owner's real memory store to an unauthenticated caller with a 200, and
    only `cli.py` ever passed `demo=True`. Derived here, once, so no caller can undo it —
    including one this function has never met."""
    mode = getattr(v2, "mode", None) if v2 is not None else None
    public_demo = mode == demo_session.DEMO_MODE
    demo = public_demo or (demo_enabled() if demo is None else demo)
    settings = settings or Settings.load()
    if public_demo:
        return build_app(settings=settings, v2=v2, mount_v1=False)
    if demo:
        settings = demo_mode.demo_settings(settings)
    # The demo keeps its own files: the point of a demo is that running it costs nothing and
    # leaves nothing behind in the owner's real memory store.
    data_dir = Path("data") / ("demo" if demo else "")
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = mem.connect(str(data_dir / "privatemesh.db"))
    mem.init_schema(conn)
    audit = AuditLog(str(data_dir / "audit.db"))
    grants = GrantStore(conn)
    # Approvals and paused turns share the memory database: both must outlive the process, and
    # the approval's `turn_id` column is what links a recovered decision to the run it paused.
    approvals = ApprovalStore(conn)
    turns = TurnStore(conn)
    registry = (demo_mode.demo_registry(conn, settings) if demo
                else build_registry(conn=conn, tavily_key=settings.tavily_key))
    llm = demo_mode.demo_llm(settings, audit=audit) if demo else TokenFactory(settings, audit=audit)
    if demo:
        demo_mode.seed_memories(conn)
    broker = Broker(registry, audit, approvals=approvals, grants=grants)
    agent = Agent(llm, broker, conn, registry, audit, turns=turns)
    return build_app(agent=agent, conn=conn, audit=audit, broker=broker, grants=grants, llm=llm,
                     registry=registry, settings=settings, v2=v2,
                     commands=MemoryCommands(conn, audit))
