"""Shared fixtures. No network: the model is a MockTransport-backed `TokenFactory` and the only
external service (Tavily) is a MockTransport `httpx.Client`."""
from __future__ import annotations

import json
import os
import re
import tempfile

import httpx
import pytest

#: FIRST, before any `privatemesh` import: the optional frozen clock has to be in place before
#: modules that do `from datetime import datetime` bind the class. Inert unless PM_FAKE_NOW is set.
from fakeclock import install_from_env

install_from_env()

#: SECOND, and before any module can call `Settings.load()`: cut the suite off from the operator's
#: own `.env` and from any provider credential already in the environment.
#:
#: `config._load_dotenv` copies `.env` into `os.environ` with `setdefault`, so a suite run from a
#: real checkout inherited the owner's real `NEBIUS_API_KEY` and `TAVILY_API_KEY` — permanently, for
#: the whole process, from whichever test happened to build `Settings` first. Two things were wrong
#: with that: a test that reached a provider would have reached the REAL one with the owner's money
#: behind it, and the suite's result depended on which directory it ran from (the demo tests, which
#: assert that demo mode refuses to start beside a real credential, went red in the owner's tree and
#: green in every worktree without a `.env`). A test that wants a credential sets one itself.
_env_file = os.path.join(tempfile.gettempdir(), "privatemesh-tests-no-such.env")
os.environ["PM_ENV_FILE"] = _env_file
assert not os.path.exists(_env_file), "the suite's env file must not exist"
for _name in ("NEBIUS_API_KEY", "TAVILY_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
              "NEBIUS_BASE_URL", "PM_MODEL_FAST", "PM_MODEL_REASON", "PM_MODEL_DEEP",
              "PM_WEB_ORIGINS"):
    os.environ.pop(_name, None)

from privatemesh import memory as mem
from privatemesh.agent import Agent
from privatemesh.audit import AuditLog
from privatemesh.broker import Broker
from privatemesh.config import Settings
from privatemesh.grants import GrantStore
from privatemesh.llm import TokenFactory
from privatemesh.skills_builtin import build_registry

S = Settings(api_key="k", base_url="https://tf.test/v1/", model_fast="nano",
             model_reason="super", model_deep="ultra", tavily_key="tvly-test")

INTENT_MARK = "You route a personal assistant"
PLAN_MARK = "PrivateMesh's planner"


class FakeTF:
    """A scripted Token Factory. Routes on the system prompt, and records what it was asked."""

    def __init__(self, *, intent=None, plan=None, answer=None):
        self.intent = intent if intent is not None else {"action": "answer", "query": "x", "steps": 0}
        # `plan` may be a list, consumed one call at a time (that is how the repair retry is
        # exercised: a bad first plan, then a good one).
        self.plans = list(plan) if isinstance(plan, list) else [plan]
        self.answer = answer
        self.calls: list[dict] = []
        handler = self._handler
        self.tf = TokenFactory(S, httpx.Client(base_url=S.base_url,
                                               transport=httpx.MockTransport(handler)))

    def _handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        messages = body.get("messages", [])
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
        self.calls.append({"model": body["model"], "system": system})
        if system.startswith(INTENT_MARK):
            content = self.intent if isinstance(self.intent, str) else json.dumps(self.intent)
        elif PLAN_MARK in system:
            content = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        else:
            content = self.answer if self.answer is not None else _echo_memories(user)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                                   "total_tokens": 7}})

    def models_asked(self) -> list[str]:
        return [c["model"] for c in self.calls]


def _echo_memories(prompt: str) -> str:
    """The default 'answer': what the model was actually shown. Makes recall observable."""
    ids = re.findall(r"- \[(\d+)\]", prompt)
    return ("using memories " + ",".join(ids)) if ids else "no memories used"


def fake_llm(**kw) -> FakeTF:
    return FakeTF(**kw)


def plan_json(*steps, goal="do the thing") -> str:
    return json.dumps({"goal": goal, "steps": list(steps)})


# ------------------------------------------------------------------ stores / kernel -----------
@pytest.fixture
def conn():
    c = mem.connect(":memory:")
    mem.init_schema(c)
    return c


@pytest.fixture
def audit():
    return AuditLog(":memory:")


@pytest.fixture
def registry(conn):
    return build_registry(conn=conn, tavily_key=S.tavily_key)


@pytest.fixture
def grants():
    return GrantStore()


@pytest.fixture
def approval_store():
    from privatemesh.approval import ApprovalStore
    return ApprovalStore()


@pytest.fixture
def broker(registry, audit, grants):
    return Broker(registry, audit, grants=grants)


@pytest.fixture
def agent(broker, conn, registry, audit):
    return Agent(fake_llm().tf, broker, conn, registry, audit)


def make_agent(conn, registry, audit, broker, tf: FakeTF) -> Agent:
    return Agent(tf.tf, broker, conn, registry, audit)


@pytest.fixture(autouse=True)
def _never_touch_the_owners_real_state(tmp_path_factory, monkeypatch):
    """No test may read or write the owner's real ~/.privatemesh (Google tokens, spend ledger) or the
    real Google client secret. Found 2026-09-18: a CLI test passed only while no owner token existed,
    and started failing the moment the owner connected their real account."""
    home = tmp_path_factory.mktemp("pm-home")
    monkeypatch.setenv("PM_GOOGLE_TOKEN_DIR", str(home / "google"))
    monkeypatch.setenv("PM_SPEND_LEDGER", str(home / "spend.db"))
    monkeypatch.setenv("PM_GOOGLE_CLIENT", str(home / "no-client.json"))
    # The owner's bearer token is a credential too — the one that opens the approve route. A live
    # wiring ISSUES one at startup, so without this override every test that builds a live product
    # would mint a token into the operator's real home.
    monkeypatch.setenv("PM_OWNER_TOKEN_FILE", str(home / "owner.token"))
    # The public demo's process-wide spend ledger — the one that enforces the demo's DAILY and TOTAL
    # caps. Missed when demo mode landed: this fixture lists variables one by one, the demo added a
    # new one, and nothing noticed. Found 2026-09-21 with 664 rows in the owner's real
    # `~/.privatemesh/demo-spend.db`, 52 of them from a single suite run. On a host that ledger is
    # the demo's budget: every local suite run was spending it, and enough runs would have made the
    # hosted demo degrade for every visitor on a budget nobody had spent.
    monkeypatch.setenv("PM_DEMO_LEDGER", str(home / "demo-spend.db"))
    # And the reason the list above can never be complete on its own: every default that is built
    # from the home directory — today `spend.py`, `demo_session.py`, `google_auth.py`, and whatever
    # is added next — resolves under THIS temporary home rather than the operator's.
    monkeypatch.setenv("HOME", str(home))
