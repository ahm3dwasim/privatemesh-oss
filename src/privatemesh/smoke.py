"""`privatemesh smoke` — three live turns against the configured .env, each printed as a receipt.

The three turns are the three things that break independently, and each one exercises a different
tier and a different gate:

  1. **memory**   — a question answered out of an approved memory (FAST intent, REASON answer).
  2. **research** — a live call out to Tavily through the broker's `web_research` tool.
  3. **approval** — `send_message`, which is HIGH risk: the turn must STOP and ask, and only run
     once the owner approves it. Auto-approving here is safe because the transport is the fake
     local one — it records to the `outbox` table and nothing leaves the machine.

It is written to be READ by whoever runs it, so every turn prints its tiers and model ids, its
latency, its token delta and the audit verdict. It is also written to be safe to run unattended,
which sets two rules:

  * **It owns its data directory** (`data/smoke/`, gitignored). A smoke run must never write into
    the owner's real memory store, so it seeds exactly one memory there — the one turn 1 exists to
    recall — and seeding is skipped when an approved memory is already present, so re-running the
    smoke does not pile up duplicates.
  * **It stops.** `SMOKE_CALL_CAP` model calls is the ceiling and the run halts at the first turn
    boundary at or past it. The check happens between turns, not mid-call: a request already in
    flight cannot be recalled, so the honest guarantee is "no new turn starts once the cap is
    spent", and the run says so rather than implying more.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import memory as mem
from .agent import NEEDS_APPROVAL, Agent
from .approval import ApprovalStore
from .audit import AuditLog
from .broker import Broker
from .config import Settings
from .grants import GrantStore
from .llm import TIER_DEFAULTS, Tier, TokenFactory, Usage
from .skills_builtin import build_registry
from .turnstore import TurnStore

#: The hard ceiling on model calls for one smoke run. Three turns at roughly two to four calls
#: each fits comfortably; anything beyond this is a runaway and the run stops instead of billing.
SMOKE_CALL_CAP = 12

SEED_MEMORY = "My favourite colour is teal."
SEED_QUESTION = "What is my favourite colour?"


@dataclass(frozen=True)
class SmokeTurn:
    key: str
    label: str
    request: str


TURNS: tuple[SmokeTurn, ...] = (
    SmokeTurn("memory", "a question answered from approved memory", SEED_QUESTION),
    SmokeTurn("research", "a live Tavily research question",
              "Search the web for what Nebius Token Factory is and tell me in one sentence."),
    SmokeTurn("approval", "send_message blocks, then runs once approved",
              "Send a message to dana@example.com saying the smoke test passed."),
)


def _snapshot(u: Usage) -> tuple[int, int, int, int]:
    return (u.calls, u.prompt_tokens, u.completion_tokens, u.reasoning_tokens)


def _delta(before: tuple[int, int, int, int], after: Usage) -> str:
    now = _snapshot(after)
    calls, prompt, completion, reasoning = (a - b for a, b in zip(now, before))
    line = f"{calls} call(s), {prompt} prompt + {completion} completion tokens"
    return f"{line}, {reasoning} reasoning" if reasoning else line


def _models_used(llm: TokenFactory, turn) -> str:
    """`phase=tier(model)` for every tier the turn used — the receipt for 'which model ran'."""
    parts = []
    for phase, tier_name in sorted((turn.tiers or {}).items()):
        try:
            parts.append(f"{phase}={tier_name}({llm.model_for(Tier(tier_name))})")
        except (ValueError, RuntimeError):
            parts.append(f"{phase}={tier_name}(unconfigured)")
    return ", ".join(parts) or "(none)"


def _verdict(audit: AuditLog) -> tuple[str, bool]:
    v = audit.verify()
    if v.ok:
        return f"ok ({v.note})" if v.note else "ok", True
    return f"BROKEN at row {v.broken_id}: {v.note}", False


def _client(settings: Settings, transport: httpx.BaseTransport | None) -> httpx.Client | None:
    """None means 'build the real one' — which is what a live smoke run wants. A transport is
    only ever supplied by the tests, so a network call cannot happen by accident in them."""
    if transport is None:
        return None
    return httpx.Client(base_url=settings.base_url, transport=transport)


def _seed(conn) -> int:
    """Put the one memory turn 1 recalls in place, approved. Idempotent across re-runs."""
    row = conn.execute("SELECT id FROM memories WHERE approved=1 LIMIT 1").fetchone()
    if row is not None:
        return int(row["id"])
    memory_id = mem.write_memory(conn, type="fact", text=SEED_MEMORY)
    if memory_id:
        mem.approve(conn, memory_id, approved=True)
    return memory_id


def build_smoke(settings: Settings, *, transport: httpx.BaseTransport | None = None,
                tavily_http: httpx.Client | None = None, data_dir: str | Path | None = None):
    """The production stack, pointed at the smoke's own data directory. Returns the pieces the
    run needs. Nothing here talks to the network — the transports decide that, and in a live run
    they are the real ones because `transport` is None."""
    directory = Path(data_dir) if data_dir is not None else Path("data") / "smoke"
    directory.mkdir(parents=True, exist_ok=True)
    conn = mem.connect(str(directory / "privatemesh.db"))
    mem.init_schema(conn)
    audit = AuditLog(str(directory / "audit.db"))
    llm = TokenFactory(settings, _client(settings, transport), audit=audit)
    broker = Broker(registry=build_registry(conn=conn, tavily_key=settings.tavily_key,
                                            http=tavily_http),
                    audit=audit, approvals=ApprovalStore(conn), grants=GrantStore(conn))
    agent = Agent(llm, broker, conn, broker.registry, audit, turns=TurnStore(conn))
    return conn, audit, llm, broker, agent


def run_smoke(settings: Settings, *, transport: httpx.BaseTransport | None = None,
              tavily_http: httpx.Client | None = None, data_dir: str | Path | None = None,
              out=print) -> int:
    """Run the three turns and print what happened. Returns a process exit code: 0 only when
    every turn ran AND every one of them left the audit chain verifiable."""
    if not settings.api_key:
        out("smoke: NEBIUS_API_KEY is not set (see .env.example) — nothing to run")
        return 1

    conn, audit, llm, broker, agent = build_smoke(
        settings, transport=transport, tavily_http=tavily_http, data_dir=data_dir)

    out(f"PrivateMesh smoke — cap {SMOKE_CALL_CAP} model calls")
    for tier in Tier:
        try:
            out(f"  {tier.value:<6} {llm.model_for(tier)}  "
                f"(max_tokens {TIER_DEFAULTS[tier].max_tokens}, "
                f"enable_thinking {TIER_DEFAULTS[tier].enable_thinking})")
        except RuntimeError as exc:
            out(f"  {tier.value:<6} UNCONFIGURED — {exc}")

    _seed(conn)

    ok = True
    for i, spec in enumerate(TURNS, start=1):
        out("")
        out(f"turn {i}/3 — {spec.label}")
        out(f"  request: {spec.request}")
        if llm.usage.calls >= SMOKE_CALL_CAP:
            out(f"  STOPPED: the {SMOKE_CALL_CAP}-call cap is spent; this turn did not start")
            ok = False
            continue

        before, started = _snapshot(llm.usage), time.perf_counter()
        try:
            turn = agent.run(spec.request)
            paused = turn.status == NEEDS_APPROVAL and bool(turn.approval_id)
            if paused:
                # The owner's half of the exchange: approve the pending action, then resume the
                # turn. This is the same pair of calls the API's /approvals/{id}/decide makes.
                out(f"  blocked, awaiting approval: {turn.approval_id}")
                broker.approvals.approve(turn.approval_id)
                turn = agent.resume_for_approval(turn.approval_id)
            elapsed = time.perf_counter() - started
        except Exception as exc:  # a smoke run reports failures, it does not raise them
            elapsed = time.perf_counter() - started
            out(f"  ERROR: {type(exc).__name__}: {exc}")
            out(f"  latency: {elapsed:.2f}s")
            ok = False
            continue

        out(f"  status:  {turn.status}" + ("  (paused, then approved and resumed)"
                                           if paused else ""))
        out(f"  tiers:   {_models_used(llm, turn)}")
        out(f"  latency: {elapsed:.2f}s")
        out(f"  tokens:  {_delta(before, llm.usage)}")
        out(f"  answer:  {' '.join(str(turn.answer or '').split())[:300] or '(none)'}")
        if turn.reason:
            out(f"  reason:  {' '.join(str(turn.reason).split())[:200]}")
        receipts = turn.receipts()
        for r in receipts:
            out(f"  step {r['step']}: ok={r['ok']} verified={r['verified']} "
                f"{' '.join(str(r['summary'] or '').split())[:120]}")
        failed = [r["step"] for r in receipts if r["ok"] is False]
        if failed:
            # A turn that "ran" while its tool returned an error has not passed a smoke test.
            # Naming the step is the whole value: it says which wire to go and look at.
            out(f"  FAILED STEP(S): {failed} — a tool returned an error; see the receipt above")
            ok = False
        verdict, verified = _verdict(audit)
        out(f"  audit:   {verdict}")
        ok = ok and verified

        if spec.key == "approval" and not paused:
            # The turn is only meaningful if the gate actually engaged — otherwise the smoke has
            # proved nothing about the thing it exists to prove.
            out("  MISS: this turn never paused for approval, so the block-and-ask path was "
                "not exercised (the model did not propose a sensitive action)")
            ok = False

    out("")
    out(f"model calls used: {llm.usage.calls}/{SMOKE_CALL_CAP}")
    out(f"tokens: {llm.usage.prompt_tokens} prompt + {llm.usage.completion_tokens} completion, "
        f"{llm.usage.reasoning_tokens} reasoning")
    final, verified = _verdict(audit)
    out(f"final audit verify: {final}")
    out(f"SMOKE {'PASS' if ok and verified else 'FAIL'}")
    return 0 if (ok and verified) else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    data_dir = None
    if "--data-dir" in argv:
        i = argv.index("--data-dir")
        if i + 1 >= len(argv):
            print("smoke: --data-dir needs a path", file=sys.stderr)
            return 2
        data_dir = argv[i + 1]
    try:
        return run_smoke(Settings.load(), data_dir=data_dir)
    except Exception as exc:
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
