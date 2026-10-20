"""The built-in skills: web research, notes, an email draft, and a deliberately fake send.

Each one declares its gate name, which is what `policy.classify_tool` / `policy.is_sensitive`
read — so the tiers are a property of the *skill*, not of a check someone remembered to write:

  web_research   WebSearch       READ    low     — nothing leaves the machine but the query
  notes_read     Read            READ    low
  notes_write    Write           DRAFT   medium  — written locally, never delivered
  email_draft    Draft           DRAFT   medium  — rendered and stored, never sent
  send_message   SendMessage     ACT     high    — an external effect: always the owner's call

`send_message` is a FAKE transport on purpose. It records to a local `outbox` table and returns
what it *would* have sent. The demo's block-and-ask moment needs a high-risk skill that the owner
approves; it does not need a real message leaving the machine, and shipping one would make the
product's promise ("it can prove it only did what you allowed") depend on the one thing it cannot
prove afterwards.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3

import httpx

from .redact import SECRET_VALUE_RE, redact_secrets
from .skills import Risk, SkillRegistry, Tool, ToolResult, guarded

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
FAKE_TRANSPORT = "fake-local-outbox"


def init_skill_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notes(
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, text TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS drafts(
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, to_addr TEXT NOT NULL,
            subject TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(
            id INTEGER PRIMARY KEY, ts TEXT NOT NULL, to_addr TEXT NOT NULL, body TEXT NOT NULL,
            transport TEXT NOT NULL, status TEXT NOT NULL);
        """
    )
    conn.commit()


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- web research ----------------
def make_web_research(http: httpx.Client | None, api_key: str) -> Tool:
    def run(args: dict) -> ToolResult:
        query = str((args or {}).get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, error="web_research needs a 'query'")
        if SECRET_VALUE_RE.search(query):
            return ToolResult(ok=False, error="refused: the query looks like a secret value")
        if not api_key:
            return ToolResult(ok=False, error="TAVILY_API_KEY is not set (see .env.example)")
        client = http or httpx.Client(timeout=30)
        try:
            r = client.post(TAVILY_URL, json={"api_key": api_key, "query": query,
                                              "search_depth": "basic", "max_results": 5})
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as exc:
            return ToolResult(ok=False, error=f"tavily returned {exc.response.status_code}")
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        answer = redact_secrets(str(data.get("answer") or "").strip())
        results = data.get("results") or []
        lines = [f"- {redact_secrets(str(x.get('title', '')))}: {redact_secrets(str(x.get('url', '')))}"
                 for x in results[:5] if isinstance(x, dict)]
        content = "\n".join(x for x in ([answer] if answer else []) + lines) or "(no results)"
        return ToolResult(ok=True, content=content,
                          meta={"sources": [str(x.get("url", "")) for x in results[:5]
                                            if isinstance(x, dict)]})

    return Tool(name="web_research",
                description="Search the web for a short factual answer with sources.",
                parameters={"type": "object", "properties": {"query": {"type": "string"}},
                            "required": ["query"]},
                risk=Risk.LOW, fn=guarded("web_research", run), gate_name="WebSearch",
                scope="web.read", retries=1)


# ---------------------------------------------------------------- notes ----------------------
def make_notes(conn: sqlite3.Connection) -> tuple[Tool, Tool]:
    def read(args: dict) -> ToolResult:
        query = str((args or {}).get("query", "")).strip()
        try:
            if query:
                rows = conn.execute(
                    "SELECT id, ts, text FROM notes WHERE text LIKE ? ORDER BY id DESC LIMIT 20",
                    (f"%{query}%",)).fetchall()
            else:
                rows = conn.execute("SELECT id, ts, text FROM notes ORDER BY id DESC LIMIT 20").fetchall()
        except sqlite3.Error as exc:
            return ToolResult(ok=False, error=f"notes read failed: {exc!r}")
        if not rows:
            return ToolResult(ok=True, content="(no notes)")
        return ToolResult(ok=True,
                          content="\n".join(f"[{r['id']}] {r['ts']} {r['text']}" for r in rows),
                          meta={"count": len(rows)})

    def write(args: dict) -> ToolResult:
        text = redact_secrets(str((args or {}).get("text", "")).strip())
        if not text:
            return ToolResult(ok=False, error="notes_write needs 'text'")
        try:
            cur = conn.execute("INSERT INTO notes(ts,text) VALUES(?,?)", (_now(), text))
            conn.commit()
        except sqlite3.Error as exc:
            return ToolResult(ok=False, error=f"notes write failed: {exc!r}")
        return ToolResult(ok=True, content=f"saved note {cur.lastrowid}", meta={"note_id": cur.lastrowid})

    read_tool = Tool(name="notes_read", description="Read the owner's local notes, optionally filtered.",
                     parameters={"type": "object", "properties": {"query": {"type": "string"}}},
                     risk=Risk.LOW, fn=guarded("notes_read", read), gate_name="Read",
                     scope="notes.read", retries=1)
    write_tool = Tool(name="notes_write", description="Save a note locally.",
                      parameters={"type": "object", "properties": {"text": {"type": "string"}},
                                  "required": ["text"]},
                      risk=Risk.MEDIUM, fn=guarded("notes_write", write), gate_name="Write",
                      scope="notes.write")
    return read_tool, write_tool


# ---------------------------------------------------------------- email / message -------------
def make_email_draft(conn: sqlite3.Connection) -> Tool:
    def run(args: dict) -> ToolResult:
        args = args or {}
        to_addr = redact_secrets(str(args.get("to", "")).strip())
        subject = redact_secrets(str(args.get("subject", "")).strip())
        body = redact_secrets(str(args.get("body", "")).strip())
        if not (to_addr and body):
            return ToolResult(ok=False, error="email_draft needs 'to' and 'body'")
        try:
            cur = conn.execute("INSERT INTO drafts(ts,to_addr,subject,body) VALUES(?,?,?,?)",
                               (_now(), to_addr, subject, body))
            conn.commit()
        except sqlite3.Error as exc:
            return ToolResult(ok=False, error=f"draft write failed: {exc!r}")
        rendered = f"To: {to_addr}\nSubject: {subject}\n\n{body}"
        return ToolResult(ok=True, content=rendered,
                          meta={"draft_id": cur.lastrowid, "sent": False})

    return Tool(name="email_draft",
                description="Write an email into the owner's drafts. This never sends anything.",
                parameters={"type": "object",
                            "properties": {"to": {"type": "string"}, "subject": {"type": "string"},
                                           "body": {"type": "string"}},
                            "required": ["to", "body"]},
                risk=Risk.MEDIUM, fn=guarded("email_draft", run), gate_name="Draft",
                scope="email.draft")


def make_send_message(conn: sqlite3.Connection) -> Tool:
    def run(args: dict) -> ToolResult:
        args = args or {}
        to_addr = redact_secrets(str(args.get("to", "")).strip())
        body = redact_secrets(str(args.get("body", "")).strip())
        if not (to_addr and body):
            return ToolResult(ok=False, error="send_message needs 'to' and 'body'")
        try:
            cur = conn.execute(
                "INSERT INTO outbox(ts,to_addr,body,transport,status) VALUES(?,?,?,?,?)",
                (_now(), to_addr, body, FAKE_TRANSPORT, "recorded"))
            conn.commit()
        except sqlite3.Error as exc:
            return ToolResult(ok=False, error=f"outbox write failed: {exc!r}")
        return ToolResult(ok=True,
                          content=f"recorded for {to_addr} via {FAKE_TRANSPORT} (nothing left the machine)",
                          meta={"outbox_id": cur.lastrowid, "transport": FAKE_TRANSPORT, "sent": False})

    return Tool(name="send_message",
                description="Send a short message to a contact. Requires the owner's approval.",
                parameters={"type": "object",
                            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                            "required": ["to", "body"]},
                risk=Risk.HIGH, fn=guarded("send_message", run), gate_name="SendMessage",
                scope="message.send")


def build_registry(*, conn: sqlite3.Connection, tavily_key: str = "",
                   http: httpx.Client | None = None) -> SkillRegistry:
    """The default skill set, wired to one SQLite store for notes/drafts/outbox."""
    init_skill_schema(conn)
    notes_read, notes_write = make_notes(conn)
    return SkillRegistry([
        make_web_research(http, tavily_key),
        notes_read,
        notes_write,
        make_email_draft(conn),
        make_send_message(conn),
    ])
