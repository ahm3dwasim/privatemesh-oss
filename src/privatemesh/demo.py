"""Demo mode — the whole product, offline, deterministic, no API keys.

`PM_DEMO=1` (or `privatemesh serve --demo`) swaps exactly two collaborators and nothing else:

  * the **model** becomes a scripted Token Factory over `httpx.MockTransport`. It is still the real
    `TokenFactory` — same client, same tier router, same token accounting — so `/usage` shows real
    bookkeeping over canned replies rather than a second implementation of it;
  * **web research** gets a mock transport with canned results, so a research step succeeds without
    a Tavily key. The skill body, its risk tier and its gate are the real ones.

Everything else — the broker, the policy table, the approval store, the hash-chained audit, the
agent loop — is the production code path. That is the point: the demo is a *seeded* PrivateMesh,
not a mock-up of one, so the receipts the UI renders are receipts the kernel actually issued.

The demo plan deliberately includes a HIGH-risk `send_message` step, because the block-and-ask
moment is the product. `CANNED_TASK` is the exact string the UI offers as a suggestion chip and the
Playwright e2e types; `tests/test_demo.py` pins it so the two cannot drift apart.
"""
from __future__ import annotations

import dataclasses
import json
import re

import httpx

from . import memory as mem
from .agent import INTENT_SYSTEM
from .config import Settings
from .llm import TokenFactory
from .planner import PLAN_SYSTEM_HEADER
from .skills_builtin import build_registry

# The two strings the UI offers and the e2e drives. Changing one without the other fails a test.
CANNED_TASK = "Tell Alex I'll be ten minutes late to the call."
CANNED_QUESTION = "What do you know about Alex?"

DEMO_KEY = "demo-no-key"
DEMO_MODEL_FAST = "nemotron-nano-demo"
DEMO_MODEL_REASON = "nemotron-super-demo"
DEMO_MODEL_DEEP = "nemotron-ultra-demo"

# Seeded synthetic memories. The third is deliberately UNapproved: the Memory screen has to show a
# pending row the owner can consent to, and the model must not be able to recall it until they do.
SEED_MEMORIES: tuple[dict, ...] = (
    {"type": "semantic", "text": "Alex is the investor contact for the seed round.",
     "entity": "Alex", "importance": 5, "approved": True},
    {"type": "preference", "text": "Prefers morning meetings, before 11am.",
     "entity": "", "importance": 4, "approved": True},
    {"type": "semantic", "text": "The Series A target is $4M.",
     "entity": "fundraising", "importance": 4, "approved": False},
)

# The verb that decides "this is an action the owner should have to approve". Deliberately broad:
# the demo's job is to reach the approval card, and a request that says "tell"/"message" is exactly
# the class of request that must never run silently.
_ACT = re.compile(r"\b(tell|message|send|text|email|ping|dm|reply)\b", re.I)
_NOTE = re.compile(r"\b(note|remember|jot|save|write down)\b", re.I)

DEMO_TAVILY = {
    "answer": "Nemotron is NVIDIA's open model family; Token Factory serves it on Nebius.",
    "results": [
        {"title": "NVIDIA Nemotron", "url": "https://developer.nvidia.com/nemotron"},
        {"title": "Nebius Token Factory", "url": "https://nebius.com/token-factory"},
    ],
}


def demo_settings(base: Settings | None = None) -> Settings:
    """The demo's settings: a placeholder key, named demo models, a mock-only Tavily key."""
    base = base or Settings.load()
    return dataclasses.replace(base, api_key=DEMO_KEY, model_fast=DEMO_MODEL_FAST,
                               model_reason=DEMO_MODEL_REASON, model_deep=DEMO_MODEL_DEEP,
                               tavily_key="demo-tavily")


# ------------------------------------------------------------------ the scripted model ----------
def intent_for(text: str) -> dict:
    """What the demo 'FAST tier' decides. Pure and deterministic — the router is testable alone."""
    if _ACT.search(text or ""):
        return {"action": "task", "query": "Alex investor contact", "steps": 1}
    if _NOTE.search(text or ""):
        return {"action": "task", "query": text[:80], "steps": 1}
    return {"action": "answer", "query": text or "", "steps": 0}


def plan_for(text: str) -> dict:
    """The demo 'REASON tier' plan. The message branch is the one that must reach the approval card."""
    if _ACT.search(text or ""):
        return {"goal": "Let Alex know the owner is running late",
                "steps": [{"description": "Send Alex a short message",
                           "tool": "send_message",
                           "arguments": {"to": "Alex (investor contact)",
                                         "body": "Running about ten minutes late — see you shortly."},
                           "expected_evidence": "the outbox records the message as recorded"}]}
    return {"goal": "Save what the owner asked to keep",
            "steps": [{"description": "Write the note to the local notes store",
                       "tool": "notes_write",
                       "arguments": {"text": (text or "").strip() or "note"},
                       "expected_evidence": "the note is stored and its id returned"}]}


def answer_for(text: str) -> str:
    """The demo 'answer tier'.

    `text` is the whole user message the agent sent, which for a turn that ran a plan also carries
    the run's results under "Run results:". The two cases get different answers because they are
    different facts: before the owner decides, nothing has run; after, exactly one action did."""
    if "Run results:" in (text or ""):
        return ("Done — it is recorded for Alex in the local outbox. It did not leave the machine: "
                "send_message writes to a fake transport on purpose, so the approval you gave ran "
                "exactly one action and the receipt above is the proof of which one.")
    # Everything with an action verb becomes a task, so this branch only ever answers a question.
    return ("Here is what I know, from the memories you approved: Alex is your investor contact "
            "for the seed round, and you prefer meetings before 11am. Anything you have not "
            "approved stays where it is — I cannot read it and neither can the model.")


def demo_llm(settings: Settings, *, audit=None) -> TokenFactory:
    """The real `TokenFactory`, over a scripted transport. Routes on the system prompt the agent
    actually sent, so a change to the agent's prompts shows up here as a wrong-branch failure
    rather than passing silently."""
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        messages = body.get("messages", [])
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = next((m.get("content", "") for m in reversed(messages)
                     if m.get("role") == "user"), "")
        if system.startswith(INTENT_SYSTEM[:60]):
            content = json.dumps(intent_for(user))
        elif PLAN_SYSTEM_HEADER[:40] in system:
            content = json.dumps(plan_for(user))
        else:
            content = answer_for(user)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 412, "completion_tokens": 96, "total_tokens": 508},
        })

    return TokenFactory(settings, httpx.Client(base_url=settings.base_url,
                                               transport=httpx.MockTransport(handler)),
                        audit=audit)


# ------------------------------------------------------------------ the scripted world ---------
def demo_registry(conn, settings: Settings):
    """The production skill set, with web research pointed at a canned transport."""
    def tavily(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DEMO_TAVILY)

    http = httpx.Client(transport=httpx.MockTransport(tavily), timeout=5)
    return build_registry(conn=conn, tavily_key=settings.tavily_key, http=http)


def seed_memories(conn) -> list[int]:
    """Write the synthetic memories once. Idempotent: a restart does not pile up duplicates."""
    if mem.recent_memories(conn, limit=1) or mem.forgotten_memories(conn, limit=1):
        return []
    ids = []
    for spec in SEED_MEMORIES:
        new_id = mem.write_memory(conn, type=spec["type"], text=spec["text"],
                                  entity=spec.get("entity", ""), importance=spec["importance"],
                                  source="demo", approved=spec["approved"])
        if new_id:
            ids.append(new_id)
    return ids
