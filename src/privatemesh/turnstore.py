"""Durable state for turns that are waiting on the owner.

The agent's turn registry was a plain dict, and the module documented the consequence as a
feature: a restart forgets paused runs, which fails closed because a forgotten run cannot be
resumed. That is true, and it is also the wrong half of the trade. Failing closed for REPLAY is
right; failing closed by silently abandoning a run the owner has already approved is not — the
owner clicks approve, the process restarts, and an action they believe was handled simply never
happened, with nothing saying so.

So a paused turn is written down: the plan, the steps already completed and their outcomes, the
approval it is waiting on, and the recalled memories the answer will be written from. Resuming
rebuilds the same frozen `Plan`/`PlanReport` objects and continues at the same step — through the
broker, exactly as before, so a restored turn has no more authority than a fresh one.

The linked approval is what makes recovery concrete: `ApprovalStore.turn_id` points at the turn,
so after a restart the owner can list the approvals that are still pending or already granted but
unspent, and each one names the turn it will resume.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from . import planner as _planner

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns(
    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
    approval_id TEXT, paused_step INTEGER, plan TEXT, report TEXT,
    memory_ids TEXT NOT NULL DEFAULT '[]', memories TEXT NOT NULL DEFAULT '[]',
    memory_lines TEXT NOT NULL DEFAULT '[]', tiers TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_turns_approval ON turns(approval_id);
CREATE INDEX IF NOT EXISTS idx_turns_status ON turns(status);
"""

_COLUMNS = ("id", "task_id", "text", "status", "answer", "reason", "approval_id", "paused_step",
            "plan", "report", "memory_ids", "memories", "memory_lines", "tiers", "updated_at")


# ---- the frozen planner types, as plain JSON ------------------------------------------------
def plan_state(plan) -> dict | None:
    if plan is None:
        return None
    return {"goal": plan.goal,
            "steps": [{"index": s.index, "description": s.description, "tool": s.tool,
                       "arguments": s.arguments, "expected_evidence": s.expected_evidence}
                      for s in plan.steps]}


def plan_from_state(state) -> "_planner.Plan | None":
    if not isinstance(state, dict):
        return None
    steps = []
    for raw in state.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        arguments = raw.get("arguments")
        steps.append(_planner.PlanStep(
            index=int(raw.get("index", len(steps))),
            description=str(raw.get("description", "")),
            tool=raw.get("tool") or None,
            arguments=arguments if isinstance(arguments, dict) else {},
            expected_evidence=str(raw.get("expected_evidence", ""))))
    return _planner.Plan(goal=str(state.get("goal", "")), steps=tuple(steps))


def report_state(report) -> dict | None:
    if report is None:
        return None
    return {"completed": bool(report.completed), "text": report.text, "reason": report.reason,
            "paused": bool(report.paused), "paused_step": report.paused_step,
            "outcomes": [{"index": o.index, "ok": o.ok, "summary": o.summary,
                          "verified": o.verified, "failure": o.failure,
                          "approval_id": o.approval_id, "content": o.content}
                         for o in report.outcomes]}


def report_from_state(state) -> "_planner.PlanReport | None":
    if not isinstance(state, dict):
        return None
    outcomes = []
    for raw in state.get("outcomes") or []:
        if not isinstance(raw, dict):
            continue
        outcomes.append(_planner.StepOutcome(
            index=int(raw.get("index", len(outcomes))), ok=bool(raw.get("ok")),
            summary=str(raw.get("summary", "")), verified=bool(raw.get("verified", True)),
            failure=str(raw.get("failure", "")), approval_id=str(raw.get("approval_id") or ""),
            content=str(raw.get("content", ""))))
    return _planner.PlanReport(
        completed=bool(state.get("completed")), outcomes=tuple(outcomes),
        text=str(state.get("text", "")), reason=str(state.get("reason", "")),
        paused=bool(state.get("paused")),
        paused_step=(int(state["paused_step"]) if state.get("paused_step") is not None else None))


def _loads(raw, fallback):
    """Parse a JSON column, falling back only when it is absent or unreadable.

    The parsed value is returned as it is — the readers below check the shape they need. Checking
    it here against `type(fallback)` would be wrong for a `None` fallback, which every non-null
    value fails."""
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


class TurnStore:
    """Turn state in SQLite. Same construction as `GrantStore`: pass a connection to share a
    database file, or take the in-memory default."""

    def __init__(self, conn: sqlite3.Connection | None = None, *,
                 path: str | None = None) -> None:
        self._lock = threading.Lock()
        if conn is not None:
            self._conn = conn
        else:
            self._conn = sqlite3.connect(path or ":memory:", check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def save(self, state: dict) -> None:
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO turns({','.join(_COLUMNS)}) "
                f"VALUES({','.join('?' * len(_COLUMNS))})",
                (state["id"], state["task_id"], state["text"], state["status"],
                 state.get("answer", ""), state.get("reason", ""), state.get("approval_id"),
                 state.get("paused_step"),
                 json.dumps(plan_state(state.get("plan"))),
                 json.dumps(report_state(state.get("report"))),
                 json.dumps(state.get("memory_ids") or []),
                 json.dumps(state.get("memories") or []),
                 json.dumps(state.get("memory_lines") or []),
                 json.dumps(state.get("tiers") or {}),
                 _now()))
            self._conn.commit()

    def load(self, turn_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return _state(row)

    def find_by_approval(self, approval_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE approval_id=? ORDER BY updated_at DESC LIMIT 1",
                (approval_id,)).fetchone()
        return _state(row)

    def drop(self, turn_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM turns WHERE id=?", (turn_id,))
            self._conn.commit()
        return cur.rowcount > 0


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now().isoformat(timespec="seconds")


def _state(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"], "task_id": row["task_id"], "text": row["text"],
        "status": row["status"], "answer": row["answer"], "reason": row["reason"],
        "approval_id": row["approval_id"], "paused_step": row["paused_step"],
        "plan": plan_from_state(_loads(row["plan"], None)),
        "report": report_from_state(_loads(row["report"], None)),
        "memory_ids": _as_list(_loads(row["memory_ids"], [])),
        "memories": _as_list(_loads(row["memories"], [])),
        "memory_lines": _as_list(_loads(row["memory_lines"], [])),
        "tiers": _as_dict(_loads(row["tiers"], {})),
    }
