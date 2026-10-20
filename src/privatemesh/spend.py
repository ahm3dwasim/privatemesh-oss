"""Hard spend cap for Token Factory calls — checked BEFORE a request is sent.

Every real call first *reserves* its worst-case cost (an over-estimate of the prompt plus the full
`max_tokens` reply) against a persistent ledger shared by every PrivateMesh process on this machine.
If the reservation would push total or today's spend past the cap, the call is refused and nothing
is sent. After the response, the reservation is settled to the provider-reported usage. A call
that fails mid-flight keeps its worst-case charge: over-counting is the safe direction.

Caps (USD): `PM_SPEND_CAP_USD` (lifetime of the ledger, default 5.00) and `PM_SPEND_DAILY_USD`
(UTC day, default 1.00). Ledger: `PM_SPEND_LEDGER` (default ~/.privatemesh/spend.db) — outside every
repo and worktree, so a new data dir or checkout cannot reset it.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

#: USD per token, from Token Factory's /v1/models?verbose=true pricing, measured 2026-09-18.
#: A model missing from this table is REFUSED — an unknown price is not a price of zero.
PRICES: dict[str, tuple[float, float]] = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (0.06e-6, 0.24e-6),
    "nvidia/Nemotron-3_5-Lightning": (0.06e-6, 0.24e-6),
    "nvidia/nemotron-3-super-120b-a12b": (0.30e-6, 0.90e-6),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00e-6, 3.00e-6),
    # The independent reviewer of a learned skill: a different model FAMILY from the generator on
    # purpose, so "a second model reviewed it" is a claim about a second set of weights rather than
    # the same one asked twice.
    #
    # UNVERIFIED. This entry was added offline (no key, no network on this lane) as a plausible
    # order-of-magnitude figure so the ledger has a line to settle against; it is NOT a measured or
    # published price. `reserve`/`settle` refuse a model with no entry, so an unpriced reviewer
    # fails loudly — but a WRONG entry would quietly mis-size the spend cap instead. Confirm both
    # rates against the provider's price list before the first live run and correct them here.
    "Qwen/Qwen3-235B-A22B-Instruct-2507": (0.20e-6, 0.60e-6),
}

#: Characters per token used to over-estimate a prompt. Real text runs ~4 chars/token; 2 makes
#: the reservation roughly double the true prompt cost, which is the direction we want to be wrong.
CHARS_PER_TOKEN_UPPER = 2.0
PROMPT_OVERHEAD_TOKENS = 64


class SpendCapReached(RuntimeError):
    """The call would exceed a spend cap. Nothing was sent."""


@dataclass(frozen=True)
class Caps:
    total_usd: float
    daily_usd: float

    @classmethod
    def from_env(cls) -> "Caps":
        return cls(float(os.environ.get("PM_SPEND_CAP_USD", "5.00")),
                   float(os.environ.get("PM_SPEND_DAILY_USD", "1.00")))


def worst_case_usd(model: str, prompt_chars: int, max_tokens: int) -> float:
    if model not in PRICES:
        raise SpendCapReached(f"no known price for model '{model}' — refusing to spend on it")
    pin, pout = PRICES[model]
    prompt_tokens = prompt_chars / CHARS_PER_TOKEN_UPPER + PROMPT_OVERHEAD_TOKENS
    return prompt_tokens * pin + max_tokens * pout


def actual_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICES[model]
    return prompt_tokens * pin + completion_tokens * pout


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


class SpendLedger:
    def __init__(self, path: str | None = None, caps: Caps | None = None):
        self.path = path or os.environ.get(
            "PM_SPEND_LEDGER", str(Path.home() / ".privatemesh" / "spend.db"))
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.caps = caps or Caps.from_env()
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10.0,
                                     isolation_level=None)
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS spend(id INTEGER PRIMARY KEY, day TEXT NOT NULL, "
            "model TEXT NOT NULL, usd REAL NOT NULL, state TEXT NOT NULL)")

    def _sum(self, where: str = "", args: tuple = ()) -> float:
        row = self._conn.execute(f"SELECT COALESCE(SUM(usd),0) FROM spend {where}", args).fetchone()
        return float(row[0])

    def totals(self) -> dict:
        return {"total_usd": round(self._sum(), 6),
                "today_usd": round(self._sum("WHERE day=?", (_today(),)), 6),
                "cap_total_usd": self.caps.total_usd, "cap_daily_usd": self.caps.daily_usd}

    def reserve(self, model: str, prompt_chars: int, max_tokens: int) -> int:
        """Reserve the worst case or raise SpendCapReached. Serialised across processes."""
        worst = worst_case_usd(model, prompt_chars, max_tokens)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                total, today = self._sum(), self._sum("WHERE day=?", (_today(),))
                if total + worst > self.caps.total_usd:
                    raise SpendCapReached(
                        f"total spend cap: ${total:.4f} spent + ${worst:.4f} worst case for this "
                        f"call > ${self.caps.total_usd:.2f} (PM_SPEND_CAP_USD)")
                if today + worst > self.caps.daily_usd:
                    raise SpendCapReached(
                        f"daily spend cap: ${today:.4f} today + ${worst:.4f} worst case for this "
                        f"call > ${self.caps.daily_usd:.2f} (PM_SPEND_DAILY_USD)")
                cur = self._conn.execute(
                    "INSERT INTO spend(day, model, usd, state) VALUES(?,?,?, 'reserved')",
                    (_today(), model, worst))
                self._conn.execute("COMMIT")
                return int(cur.lastrowid)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def settle(self, reservation: int, model: str, usage: dict | None) -> None:
        """Replace the reservation with the reported cost. No usable usage -> keep worst case."""
        u = usage or {}
        p, c = u.get("prompt_tokens"), u.get("completion_tokens")
        if not isinstance(p, int) or not isinstance(c, int):
            self._conn.execute("UPDATE spend SET state='unsettled' WHERE id=?", (reservation,))
            return
        with self._lock:
            self._conn.execute("UPDATE spend SET usd=?, state='settled' WHERE id=?",
                               (actual_usd(model, p, c), reservation))

    def release(self, reservation: int) -> None:
        """Give back a reservation that was never spent. Nothing was sent, so nothing is owed.

        Its one caller is `demo_session.ChainedLedger`, which reserves against two ledgers in
        order and must not leave the first one charged when the second refuses: a cap that counted
        money for a call that never happened would ratchet the demo's global budget down every time
        a visitor hit their own session cap, and the failure would look like the demo "running
        out" for no reason a reader of the ledger could see.

        `AND state='reserved'` is the safety: a settled row records a call the provider really
        answered and is never erased. Deleting it would lose the only record that money was spent.
        """
        with self._lock:
            self._conn.execute("DELETE FROM spend WHERE id=? AND state='reserved'", (reservation,))
