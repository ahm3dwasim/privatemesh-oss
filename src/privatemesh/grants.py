"""Grants — the owner's per-skill ceiling, stored where the owner can change it and the model can't.

A grant maps a skill name to `auto` (run it), `approve` (always ask) or `deny` (never). It is the
data behind `policy.AutonomyPolicy.tool_overrides`: a grant can only tighten what the fixed
decision table allows, or promote a non-sensitive action. It can never promote a sensitive or
BLOCKED one — `policy.evaluate` checks those first, which is why this store needs no safety logic
of its own beyond validating the mode string.
"""
from __future__ import annotations

import sqlite3
import threading

from .policy import AutonomyPolicy

MODES = ("auto", "approve", "deny")
DEFAULT_MODE = "approve"


class GrantStore:
    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._lock = threading.Lock()
        self._conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS grants(
                skill TEXT PRIMARY KEY, mode TEXT NOT NULL, updated_at TEXT NOT NULL);
            """
        )
        self._conn.commit()

    def set(self, skill: str, mode: str) -> str:
        """Record the owner's ceiling for one skill. Returns the stored mode."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
        if not skill:
            raise ValueError("a grant needs a skill name")
        import datetime as _dt

        with self._lock:
            self._conn.execute(
                "INSERT INTO grants(skill,mode,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(skill) DO UPDATE SET mode=excluded.mode, updated_at=excluded.updated_at",
                (skill, mode, _dt.datetime.now().isoformat(timespec="seconds")))
            self._conn.commit()
        return mode

    def clear(self, skill: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM grants WHERE skill=?", (skill,))
            self._conn.commit()
        return cur.rowcount > 0

    def all(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT skill, mode FROM grants ORDER BY skill").fetchall()
        return {r["skill"]: r["mode"] for r in rows}

    def policy(self) -> AutonomyPolicy:
        """The `AutonomyPolicy` these grants describe. `medium_action` stays at its safe default
        (ask): a grant is per-skill and explicit, and a blanket "auto everything routine" switch
        is exactly the kind of widening this product exists to make visible instead."""
        return AutonomyPolicy(medium_action="approve", tool_overrides=self.all())
