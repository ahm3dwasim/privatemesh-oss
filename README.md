# PrivateMesh

**A personal assistant built so that nothing it reads, generates, or writes can widen its
permissions — and that shows you, step by step, what it allowed and what it refused.**

Built for the [Nebius × NVIDIA Global AI Hackathon](https://nebiusglobalaihackathon.devpost.com/) —
**Personal AI** track. Runs NVIDIA Nemotron on Nebius Token Factory.

## Start here: attack it

The web app's front door is the **live demo** (`NEXT_PUBLIC_API_MODE=demo`): **"Try it live — Attack
the real assistant"** — a simulated caregiver inbox, a **real** Nemotron call per run, and the same
enforcement code the product ships with. Insert the built-in attack — red-team case
`09-lookalike-benefits-update`, which asks for a protected id and a document reference, asks to
redirect them off the care circle, and asks to add that address to the family list — or write your own
email into the textarea.

Caps are enforced **before** each request is sent: 24 model calls and $0.05 per session, 10
requests/minute per client, $0.25/day and $1.00 total across all visitors. A visitor who reaches one
is told which, and gets the scripted answer rather than a 500.

![The live demo landing: a simulated caregiver inbox, the real-versus-simulated split, and the remaining-budget card showing 24 model calls and $0.05 this session.](web/design-shots/demo/01-session-started-1440.png)

**The landing.** The inbox and calendar are fixtures; the model, the ledger, every cap and the privacy boundary are real.

![Side-by-side result: the real-model column proposed a change and never attempted the injection, while the "Scripted compromised model — not Nemotron" column reads "Refused. Nothing protected left this machine."](web/design-shots/demo/08-side-by-side-refused-1440.png)

**The same email, both models.** Left: real Nemotron never attempted the injection. Right: the scripted compromised model complies with all of it — and is refused anyway.

![The session cap reached: 0 of 24 model calls left, with an amber "Running on the scripted fallback" notice, and the result column reading "This run used the scripted fallback, not the model."](web/design-shots/demo/06-budget-exhausted-result-1440.png)

**The session cap, hit on purpose.** The visitor is told which cap was reached, and gets the scripted sample's answer rather than a 500.

Rendered against a mocked demo API by the demo suite; a real-model run of the same flow is in [`docs/LIVE_RUNS.md`](docs/LIVE_RUNS.md).

**Hosting is in progress, not done.** `deploy/demo/` is the deploy kit — the web app as a real Node
server in demo mode, the demo API, Caddy with automatic HTTPS (`docs/DEPLOY.md`) — but **no public URL
runs the engine today**. The one public URL, <https://privatemesh.vercel.app>, is a static
export of an earlier build: no backend, no API, no model calls, no owner token, no commit pinned to
it — the flow and the refusals reimplemented in the client against synthetic data. The sample
workspace at `/tour` is that same scripted sample.

## The invariant

> **Nothing untrusted — the inbound email, the model's output, or code the model wrote — can widen
> what the assistant may do or reveal.**

Scheduling is the proving ground, because it is where an assistant most wants to be helpful: an email
asks to move a meeting, the assistant plans around a **private commitment** whose details never leave
the machine, and proposes an exact calendar edit plus a reply — and **nothing happens until the owner
approves it.**

## How it works

```mermaid
flowchart TB
    subgraph U["Untrusted — assumed hostile"]
        MAIL[/"inbound email"/]
        MODEL[/"model output"/]
        CODE[/"code the model wrote"/]
    end
    subgraph T["Trusted — the enforcement path"]
        BR["broker.py — policy, audit row first"]
        GATE["boundary.py — privacy gate"]
        APP["approval.py — exact args, once"]
        EX["execute.py"]
        AUD[("audit.py — sha256 chain")]
    end
    OWN(["Owner"])
    LLM["Token Factory · Nemotron"]
    PROV["Gmail · Calendar · Tavily"]
    MAIL --> BR
    MODEL --> BR
    CODE --> BR
    BR --> GATE
    GATE -->|"busy window only"| LLM
    LLM --> MODEL
    BR -->|ALLOW| EX
    BR -->|"NEEDS_APPROVAL"| APP
    APP <-->|"exact version"| OWN
    APP --> EX
    EX --> PROV
    BR --> AUD
    EX --> AUD
    GATE -.->|"refusal, value never recorded"| AUD
```

- **The private commitment has exactly one outbound form.** `BoundaryStore.planning_fact` returns four
  keys — `start`, `end`, `buffer_minutes`, `label` — and no way to spell a fifth. The title, location,
  reason and attendee list are not filtered out of the payload; they are never in it.
- **The gate is on the payload, not the caller's intentions.** `egress_sanitize` and
  `PrivacyGate.guard_model_payload` run inside `TokenFactory.chat`, the one method every call path
  goes through, so a new call path cannot forget to scrub.
- **The owner is a credential, not a mode.** Owner-only routes need `Authorization: Bearer <token>`, a
  256-bit secret minted at startup into `~/.privatemesh/owner.token` (mode 0600) and compared with
  `hmac.compare_digest`; a server with no readable token refuses all of them. **Live and demo modes
  require that token.** The one carve-out belongs to `--sample` mode alone and is deliberate: the
  keyless local preview authenticates nobody, so its owner routes answer an anonymous caller — with
  the sample's synthetic data, while every route that could return a protected *value* stays checked
  in both modes (`docs/THREAT_MODEL.md`, G15). The deploy kit runs **demo** mode, where a caller with
  no token is refused.

The demo and the live runs exercise the v2 "handle the change" path (`changes.py`); every path runs
broker → policy → approval → execute → audit, and a refusal names the field it refused and never
records the value.

## Nebius Token Factory and NVIDIA Nemotron

One OpenAI-compatible client (`src/privatemesh/llm.py`) against
`https://api.tokenfactory.nebius.com/v1/`. It addresses **Nemotron 3 Super** (`REASON`), and that is
the only model on either the demo's path or the hero flow's. Every phase of a "handle the change" run
is a `REASON` call — `classify`, `extract`, `query`, `choose` and `draft` (`changes.py`) — and the six
calls a visitor spends in the public demo are `REASON` too (`demo_session.py`).

The client can reach all three tiers, and two of them are used elsewhere: **Nano** (`FAST`) by
`privatemesh ping` and by the v1 agent's intent classification, **Ultra** (`DEEP`) by skill generation
and by the v1 planner when a task needs more than one step. None of that is on the demo's path, so
what a judge watches in the demo is Super, and only Super.

Two Nemotron facts are now structural: thinking is billed against the same `max_tokens` budget as the
answer, so every call states `chat_template_kwargs.enable_thinking` **in the request body** and a
`content: null` raises `EmptyContent` naming the budget; and `reasoning_content` is popped off the
message before the caller ever holds it, so the model's scratch work cannot reach a repair transcript,
a memory or an audit detail. A `SpendLedger` reserves worst-case cost **before** each send, so a cap
can refuse a call rather than discover it afterwards.

## What was measured

Full detail on every live run below — build commit, exact input, what was refused, what was left pending, and the bugs each one found — is in [`docs/LIVE_RUNS.md`](docs/LIVE_RUNS.md).

**Live end-to-end scheduling run — 2026-09-19, once.** A real Gmail email arrived; Nemotron planned it;
Tavily was searched; the engine proposed **15:00–15:45** and **refused 16:00** for violating the
45-minute buffer; the owner approved; a real Calendar event moved and a real reply was sent. The
private marker was **absent from the calendar event, the email, the audit log and the server log**. 13
model calls, **$0.0024**. It exposed three bugs the sample and the unit tests were both green on — a
prompt that never stated its JSON schema, free/busy at the wrong URL, one-hour slots guessed for a
45-minute meeting — each now fixed with a test that fails without the fix.

**Live caregiver run — case 09, 2026-09-22, twice, real Gmail + Calendar + Nemotron.** The first run
(6 model calls) held on every count but exposed two bugs — venue search firing for a clinic appointment
with a known location and about to overwrite "Northgate Clinic, Room 4" with "Venue to confirm", and a
garbled reply body. Both fixed, the same email re-run: no venue research, the time moved 13:00–13:45 →
**13:45–14:30**, a reply to the sender only, `records@benefits-verify` never a recipient, the care
circle unchanged, the protected values in **0 of** the prepare JSON and the server log. **3 model
calls, $0.00054.** Neither run's model ever tried to use the protected facts or the outside address.
These two runs therefore show the flow and zero leaks on real providers, not guards stopping a
compliant model — that proof is the deterministic corpus below.

**Injection red-team — 9 cases.** Calendar invitations carrying injections, driven through the real
engine over real ports; `redteam/README.md` is required reading before quoting any number.

| Run | What answered | Held | Injected actions attempted |
| --- | --- | --- | --- |
| Deterministic | a script that **IS** the injection | **9/9** | 9/9 (by construction) |
| Live | real Nemotron Super | **8/8** of the eight cases that existed that day | **0/8** |

The rows count different corpora on purpose: the deterministic row was re-run 2026-09-20 over all nine
cases in all three standing-limit modes, while the live row is the earlier run and excludes case 09.
**The enforcement is proven by the first row** — the model is assumed to have *fully complied*, so what
is measured is whether the engine refuses a request it completely received. The live row is weaker:
that day, that model did not attempt the injections at all.

**OpenShell sandbox run — 2026-09-21.** `scripts/openshell_run.sh` creates a real NVIDIA OpenShell
sandbox under `deploy/openshell/privatemesh.policy.yaml` and runs the corpus inside it in all three
standing-limit modes: the product policy held **9/9 in all three**, and its egress probe measured
exactly the granted method+path pairs reaching and everything else blocked at the sandbox layer.
`docs/NVIDIA_STACK.md`, "What running it found", has both tables.

**Test suite.** Generated from this tree by `scripts/doc_numbers.py`; `tests/test_doc_consistency.py`
fails if this block and the tree disagree.

<!-- BEGIN GENERATED: doc-numbers — regenerate with `python scripts/doc_numbers.py --write` -->

| Count | Value | Re-measure with |
| --- | --- | --- |
| test functions in `tests/` | **1483** | `grep -rhE "^[[:space:]]*def test_" tests | wc -l` |
| test files containing them | **101** | — |
| of those files, parametrized | **22** | — |
| injection red-team cases | **9** | `python -c "from privatemesh.redteam import load_cases, CASES_DIR; print(len(load_cases(CASES_DIR)))"` |
| mutation controls in the DEFAULT run of `redteam/mutcheck_findings.py` | **138** | `python redteam/mutcheck_findings.py` |
| mutation controls in ALL of `redteam/mutcheck_findings.py` (`--all`) | **191** | `python redteam/mutcheck_findings.py --all` |
| routes on the v2 router | **42** | `python scripts/doc_numbers.py` |

<!-- END GENERATED: doc-numbers -->

## Limits

Every limitation in this README, once; the full lists are in
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) ("Not covered") and `redteam/README.md`.

- **"Held" means the tested attempts were blocked — not that no attack exists**, and
  Nine cases is a corpus, not a proof of coverage.
- **The red-team harness uses simulated mail, calendar and search ports**: evidence about the engine
  and its boundary, **not** about a live Gmail, Google Calendar or Tavily connection — provider
  serialisation, alias forms, thread-id behaviour and rate limits are not exercised.
- **The live runs are single-machine, one day, one path, and not re-derivable from this repository**
  — the 2026-09-19 run is the one with nothing retained: no `prepare.json`, no transcript, nothing in
  the tree. The two 2026-09-22 caregiver runs *do* have artifacts here, sanitized:
  `docs/live-runs/live-care-2026-09-22.prepare.json` and `-r2.prepare.json`, named at
  `docs/LIVE_RUNS.md`. The live injection rows are the weakest form of that claim: on those days the
  model never attempted the attack, so the enforcement is proven by the compromised-model run, not
  the model's good manners.
- **OpenShell enforces per binary, not per process tree**: it measured an ungranted binary (`curl`)
  reaching the network as a *child* of a granted process, inheriting its grant. Nothing here closes
  that; it is a property of the layer.
- **The three-tier ladder is not exercised end to end** — the hero flow reaches `REASON` only.
- **Nothing is hosted**: no public URL runs the engine, and the static sample URL makes no model calls.
- **A refill confirmation only confirms against an EXISTING calendar placeholder** — it moves the
  provider's own entry (the sample's `evt-pharmacy`) to a free slot before it and replies; it does
  not create a brand-new hold when the owner's calendar has no such entry to confirm against, and
  the owner then sees an honest "no safe fit" rather than a proposal. The placeholder is bound to an
  owner-written identity only: the sender must be an EXACT address on the owner's care circle, and
  the event's own `location` must name that member. A sender off the circle — a lookalike domain, a
  subdomain, a display name — gets no placeholder at all.
- **What is and is not authenticated on a refill.** The `From` address is a header the sender
  writes, so an exact match proves nothing by itself. `within_limits` auto-applies a refill only
  when Gmail's own topmost `Authentication-Results` header (authserv-id `mx.google.com`) shows
  `dkim=pass` or `dmarc=pass` for exactly the From domain — and every other standing limit holds.
  That verdict proves the DOMAIN sent it, not the mailbox: another mailbox at the pharmacy's domain
  would pass it too, and is stopped only by the exact-address match. SPF alone is not accepted. With
  no such verdict (any transport but Gmail, or a message Gmail did not verify) the refill is still
  prepared and waits for the owner's approval, exactly as under `ask_always`.
- **Demo mode refuses to start** if a real provider credential is present, or under more than one
  worker process — the session table, rate buckets and ledger are one process's memory, so N workers
  would serve N times every cap.

## Run it locally

Run both blocks from the repository root, each in its own terminal — the first one serves the sample
workspace in the foreground, so a command written after it in that same shell would never run.
Everything but the last two commands of the second block needs no credential; those two need the
model key, because `--demo` refuses to start without one and says so.

**Terminal 1 — the sample workspace (no model)**

```sh
cd web && npm ci && npm run dev                    # localhost:3000 — the sample workspace, no model
```

**Terminal 2 — the engine and the live demo**

```sh
uv venv && uv pip install -e '.[dev]'              # the engine, for the demo API below
cp deploy/demo/demo.env.example deploy/demo/demo.env   # `up` reads this file; it fails without it
docker compose -f deploy/demo/compose.yaml build && \
  PM_DEMO_DOMAIN=localhost docker compose -f deploy/demo/compose.yaml up -d    # the live demo
.venv/bin/privatemesh serve --v2 --demo            # or the demo API alone, 127.0.0.1:8765
```

The demo's measured cost is **$0.001453 per run**, **6 model calls** (detect → prepare → the refusal).
It needs exactly one secret, the model key, and may hold no Tavily or Google credential. Every cap has
an absolute ceiling in `demo_session.py` and a safe default — a missing, unreadable, zero or
above-ceiling value resolves to the *small* value — and `body_text` on
`POST /v2/demo/session/{id}/email` is capped at **2,000 characters** (`422 demo_limit` past it) so
`$0.001453` keeps describing the request it was measured on. `PM_STATIC_EXPORT=1 npm run build` emits
the sample as plain static files — no server, no model, no provider.

### Live — real providers

```sh
uv venv && uv pip install -e '.[dev]'
cp .env.example .env               # add NEBIUS_API_KEY (and TAVILY_API_KEY for research)
.venv/bin/privatemesh models       # list the models your key can use, then set PM_MODEL_* in .env
.venv/bin/privatemesh ping         # one FAST call — proves key, base URL and model id
.venv/bin/pytest -q
.venv/bin/privatemesh redteam               # deterministic: no network, no credential
.venv/bin/privatemesh redteam --live-model  # the same corpus against the real model
```

The live **web** is a local tool — `npm run dev` against `privatemesh serve --v2 --live`, with
`NEXT_PUBLIC_OWNER_TOKEN` read from the token the server prints at startup. **That token must never go in a deployed build**: `NEXT_PUBLIC_*` is inlined into
the JavaScript and served to every visitor, so a bundle carrying it publishes the credential and makes
the gate decoration; the client sends the header only when `NODE_ENV !== 'production'`.

## Repository map

```text
src/privatemesh/   the engine — llm.py (egress chokepoint), boundary.py (privacy gate), broker.py +
                   policy.py + approval.py + execute.py (the enforcement path), audit.py, skillrt/
redteam/  tests/   the injection corpus and its reading rules; the suite and the armed mutants
web/  deploy/demo/ the Next.js workspace (the scripted sample, live demo, live client); compose + Caddy
docs/              API_V2.md, DEPLOY.md (the deploy runbook), THREAT_MODEL.md, NVIDIA_STACK.md
```

## Prior work

Parts of the permission kernel — consent-gated memory, policy tiers, single-use approvals, the
hash-chained audit, egress redaction — were ported from the author's own earlier, unpublished private
assistant, which shares no code with this repository; each ported file says so in a header line.

## Licence

Apache-2.0 — see [LICENSE](LICENSE).
