# API v2 contract — "Handle the change"

The web app (lane ui2) builds against this; the engine (lane kernel2) implements it. JSON over HTTP,
FastAPI. All times ISO-8601 with offset. `mode` is "sample" (synthetic data, simulators, labelled),
"live", or "demo" (a real model, simulated providers, hard caps — public routes, no owner token;
see [the public demo](#the-public-demo--post-v2demosession-and-its-three-siblings-four-public-routes-2026-09-20)).

| Method + path | Returns / does |
|---|---|
| GET /v2/session | {mode, owner_name, timezone, connections: [{service: gmail\|calendar\|tavily\|nemotron, state: disconnected\|authorizing\|connected\|expired, detail}], routine: {enabled, paused, last_checked_at}} |
| POST /v2/routine {paused: bool} | pause/resume the meeting-changes watcher |
| GET /v2/today?date=YYYY-MM-DD | {date, timezone, events: [CalendarBlock], protected: [ProtectedBlock], changes: [ChangeSummary]} |
| POST /v2/changes/{id}/prepare | starts preparation; returns full Change |
| GET /v2/changes/{id} | Change (below); poll only during reading_thread, checking_availability, checking_venues, approved, or executing |
| POST /v2/changes/{id}/leave | leave the meeting unchanged |
| POST /v2/changes/{id}/edit {reply_body?, start?, venue_index?} | new proposal version; invalidates any prior approval |
| POST /v2/changes/{id}/approve {version} | approves the exact bundle version; 409 if stale |
| GET /v2/changes/{id}/outcome | per-effect states (below) |
| GET /v2/changes/{id}/why | {used: [...], kept_private: {categories, counts}, changed: [...], technical: {model_calls, audit_ids}} |
| GET /v2/changes/{id}/model-view | owner-token-gated like /decisions, 404 for an unknown change: {change_id, calls: [ModelCall], kept_on_machine: {categories, counts}} |
| POST /v2/playground/attack {email} | **sample only — 404 in live**: runs the email as an inbound attack through the real sample engine and returns PlaygroundResult (below) |
| POST /v2/demo/session | **demo only — 404 elsewhere**: starts a public session. `{session_id, mode, real, simulated, notice, budget, caps, spend, inbox}` (below) |
| GET /v2/demo/session/{id} | **demo only**: that session's state re-read — budget left, calls left, the seeded inbox |
| POST /v2/demo/session/{id}/email {message_id, body_text} | **demo only**: rewrite an inbound email and run the real pipeline over it. `{session_id, change_id, budget, spend, change, model_view, decisions, model: {kind: "real"}}`. `body_text` is capped at 2,000 characters — `422 demo_limit`, nothing ran, past it |
| POST /v2/demo/session/{id}/email/compromised {message_id, body_text} | **demo only**: the SAME email, through the red-team corpus's own fully-compliant scripted model instead of the real one — same engine, gate and boundary, zero model calls, zero spend. Same shape as `/email` plus `{compromised: true, attempted: [str], model: {kind: "scripted", label: str, calls: 0, cost_usd: 0}}`. Rate-limited like every other demo route; refuses the same way `/email` does for an empty or over-length body |
| GET /v2/preferences / POST / DELETE /v2/preferences/{id} | consented facts with scope, source, last_used |
| GET /v2/plans | task list with state completed\|needs_you\|partial\|stopped |

Errors: `409` when the reviewed plan is no longer the plan (the version moved, the invite moved, or the
protected commitment changed after the review) — nothing was sent; `422` when the request itself is refused
(a time that does not fit, or a payload the privacy gate will not let out), with the reason in `detail`;
`503` when the provider connection is gone (an expired or revoked Google token) — the detail names the one
action that fixes it, nothing was sent, and retrying before a human reconnects cannot succeed.

CalendarBlock {id, title, start, end, kind: "event"\|"proposed"\|"confirmed"}
ProtectedBlock {id, start, end, buffer_minutes, label: "Protected"}   — never the private title
ChangeSummary {id, from_name, from_address, received_at, excerpt, thread_url, stage}
Change {id, version, stage: "detected"\|"reading_thread"\|"checking_availability"\|"checking_venues"\|"proposal_ready"\|"no_safe_fit"\|"approved"\|"executing"\|"done"\|"partial"\|"stopped",
        request_excerpt, proposal: {start, end, venue: {name, address, evidence: Evidence, note}, rejected: [{name, reason, evidence}],
        reason_sentence, before: [CalendarBlock], after: [CalendarBlock], protected: ProtectedBlock,
        actions: [{kind: "calendar_update", event_id, attendees: [full addresses], diff: {start: [old,new], end: [old,new], location: [old,new]}, notifies_attendees: bool, notifies: [full addresses]|null},
                  {kind: "email_reply", to: [full addresses], subject, body}],
        privacy_line: "Your 16:30 commitment stays protected. Its details are not included.",
        expires_at}}
Outcome {effects: [{kind, state: "pending"\|"running"\|"done"\|"failed"\|"unknown", provider_url?, detail}], summary}
Evidence {url, title, excerpt, retrieved_at, official}

## UI v2 additions (2026-09-18)

`web/lib/v2-types.ts` provides the strict UI types. These additions support the exceptional states.
The synthetic adapter makes zero network/provider calls. `NEXT_PUBLIC_API_MODE` defaults to sample;
`live` uses HTTP. Optional `NEXT_PUBLIC_API_BASE_URL` prefixes `/v2`. No automatic effect retries.

- Session adds `authenticated_owner: boolean` (missing means false) and
  `boundary: {start: "HH:MM", buffer_minutes: number, configured: boolean}`. Live approval must also
  enforce owner authorization server-side. Routine `last_checked_at` may be null.
  **`authenticated_owner` answers about THIS REQUEST, not about the process** (2026-09-19): the
  server mints an owner token at startup into `~/.privatemesh/owner.token` (0600;
  `PM_OWNER_TOKEN_FILE` overrides) and the owner-only routes — prepare, leave, edit, approve,
  decisions, routine, boundary, preference writes, learn/approve/reject/revoke — require it as
  `Authorization: Bearer <token>`, compared with `hmac.compare_digest`. A live server with no
  readable token refuses all of them. Sample mode has no token: its owner is the person running the
  demo, on their own machine, against synthetic data. The live web client sends the header from
  `NEXT_PUBLIC_OWNER_TOKEN` **only in a non-production build** — the live web is a local tool, and
  a `NEXT_PUBLIC_*` value is inlined into the bundle it is served from.
- `POST /v2/boundary {start,buffer_minutes}` returns Session.
- `POST /v2/connections/{service}` returns Session. Sample connects without OAuth. A live engine must
  implement its authorized connection flow and confirm provider success; the UI never collects keys.
- POST routine returns Session. Prepare, leave, edit and approve return full Change.
- Change adds `blocked_reason: string|null`, `updated_at: ISO timestamp`; proposal is null until ready.
  Preparation refuses missing/expired access and paused routines; stopped/executing changes cannot approve.
- Proposal adds `venues: Venue[]` with checked alternatives. Venue edits update calendar location and
  reply; all edits increment version and refresh expiry. Time edits must recheck the request, duration,
  protected time and buffer. Rejected candidates may have null evidence for calendar boundary checks.
- Evidence adds `sample: boolean`; URL may be null for synthetic evidence. ChangeSummary thread_url
  may likewise be null. Sample venue hours are explicitly synthetic, never claims about real venues.
- Error JSON: `{detail: string, code: string}`. Codes: stale_version (409 for version mismatch or expiry),
  invalid_stage (409), connection_required (409), routine_paused (409), approval_forbidden (403),
  decisions_forbidden (403), owner_forbidden (403 for every other owner-only route),
  boundary_conflict (422), not_found (404), rate_limited (429, playground only),
  demo_unavailable (503, demo only — the session limit, or no model key),
  demo_empty_email (422, demo only — nothing ran and no call was spent),
  playground_body_too_large (413, playground only, refused before the body is parsed), playground_limit
  (422 for an oversized email, 503 for a run that spent its wall-clock budget).
  Stale approvals produce no effects; reload and review anew.
  Unknown effect means unconfirmed, not safe-to-retry failure. Partial/unknown effects never auto-retry.
- Why: `used: {label,detail}[]`, `kept_private: {categories:string[],counts:Record<string,number>}`,
  `changed:string[]`, `technical:{model_calls:number,audit_ids:string[]}`.
- Preferences GET returns Preference[]; POST accepts `{fact,scope,source}` and returns Preference;
  Preference is `{id,fact,scope,source,last_used: ISO timestamp|null}`. DELETE returns 204.
- Plans GET returns `{id,title,state,detail}[]`.

### Canonical sample

Tomorrow is fixed to 19 September 2026, Europe/London. Alex says: “Our usual café has closed. Could we meet near King’s Cross tomorrow at 3 or 4? You choose and update the invite.” The original 45-minute meeting is 11:00–11:45 at the usual café. Corner House Café is rejected for closing at 15:00. A 16:00 start
conflicts with the protected 16:30 block and 45-minute buffer. Fern & Finch Café at 15:00–15:45 is
proposed. Private event titles/locations are never available to this sample or included in the reply.
Venue names, addresses, hours and outcomes are synthetic; the UI labels the fixed sample date.

`resetSample(variant)` supports happy, partial, unknown, stale, no_safe_fit, expired, missing, paused,
and empty. Preparation advances by elapsed time through reading_thread, checking_availability,
checking_venues, proposal_ready (or no_safe_fit); approval advances approved, executing, done (or partial).
Partial means calendar done/reply failed; unknown means calendar done/reply unconfirmed. The stale
fixture increments version at first approval and refuses it so fresh review can be exercised locally.

Preference editing uses `PATCH /v2/preferences/{id} {fact,scope}` and returns the updated Preference,
retaining its stable ID, original consent source and last_used timestamp. Missing IDs return 404;
empty fact or scope returns 422. Sample additionally supports authorizing, expired_connection (expired provider access; expired is instead an expired proposal), and long_wait (preparation remains at checking_venues until the user stops it).

Sample boundary updates accept valid times and buffers from 0–240 minutes, invalidate pending proposals, and recheck safe fit on preparation. Preparation may regenerate stopped, ready or no-safe-fit proposals with a new version. Expired proposals have connected accounts; preparing again refreshes their expiry. Calendar updates notify attendees; venue seating is explicitly not reserved.

### Notification, provenance, and stale-review detail

Calendar actions include `attendees: string[]` independently of email `to` recipients. A UI must not
infer calendar notification recipients from an email action. Missing calendar attendees prevents approval
when notifications are enabled. `notifies` is the audience that list implies — the same addresses when
notifications are on, `[]` when they are off, and `null` when this build did not record the guest list.
A review screen must render `null` as "not recorded" and never as "nobody": the two are different
claims, and only one of them is safe to show. `Why.used[]` may include `evidence: Evidence`; `technical`
may include `model_call_records: {status: attempted|provider_confirmed, model, id}[]`. A model attempt is
not evidence of provider success. The sample has zero attempts and an empty model call record list.
Private-context count keys exactly match the category labels.

Consented preferences whose scope is `Meeting planning` are included in sample preparation; preparation
records their last_used and captures the facts in Why. Other scopes are excluded. Revocation prevents use
in future preparations. Historical action provenance is retained separately from the preference list.

The stale fixture changes the original calendar meeting from 11:00–11:45 to 12:00–12:45 while retaining
the proposed 15:00–15:45 interval. It updates the before blocks and calendar action old-value diffs,
increments the version, and provides a field-specific blocked_reason. Fresh preparation keeps that new
baseline. Changes to boundary/time/venue update reason, outgoing generated reply, outcome, and Why;
explicit owner-supplied reply text remains authoritative.

### Public preview (client-owned, no backend endpoint)

`api.preview()` returns `{today: Today, change: Change}` containing a freshly generated synthetic,
prepared canonical proposal. Both sample and live clients use this local illustration: it never accesses
providers, mutates a task stage, or reads live owner data. It must remain visibly labeled as sample data
on the public entry screen. The helper does not construct an adapter or replace the sample reset target.
Sample connection authorization enters `authorizing` and confirms on session polling after 1.5 seconds;
this is only a simulation. A missing-connection fixture starts with routine.enabled=false. Explicitly
resuming the routine sets enabled=true.

Why may additionally return `effects: [{kind, state: prepared|confirmed|failed|unknown,
fields: Record<string,string|string[]>, provider_id?, provider_url?}]` for exact outgoing field
manifests and provider result identifiers. `technical.audit_hashes?: {algorithm,value}[]` contains
available audit hashes. Only a verified provider result may use `confirmed` in live mode. Synthetic
sample effects have no provider IDs/URLs and no cryptographic hashes; the sample label remains visible.

## Concept D — "Teach it once. Keep control." (manager contract, 2026-09-18)

A **skill** is generated code that COMPUTES (e.g. the meeting-option evaluator: offered slots + venue evidence +
minimum notice + buffers + permitted availability -> ranked options with reasons). It never performs effects;
effects stay with the trusted connectors and the change-set approval. Runs in src/privatemesh/skillrt.

| Method + path | Returns / does |
|---|---|
| GET /v2/skills | [SkillSummary] — the owner's library: name, purpose, version, identity (sha256), state, learned_at, last_used_at, uses, rule |
| POST /v2/changes/{id}/learn | when a change needs a capability the library lacks: start learning; returns {learning_id} |
| POST /v2/teach {rule} | Teach Mode (below): the owner's own sentence becomes a skill through the same loop; returns {learning_id} |
| GET /v2/learning/{id} | Learning (below); poll until stage is "ready_for_review" or "failed" |
| POST /v2/learning/{id}/approve {identity} | install exactly this identity (409 if the identity changed); separate from approving any effect |
| POST /v2/learning/{id}/reject | discard |
| POST /v2/skills/{name}/revoke | disable a skill; future plans cannot use it |

Learning {id, change_id, skill_name, purpose_plain, stage: "generating"|"testing"|"reviewing"|"revising"|"ready_for_review"|"failed",
  attempts: [{version, tests: {passed, failed}, held_out: {passed, failed}, violations: [{kind, detail}],
              review: {verdict: "approve"|"reject", reasons: [str], reviewer_model}}],
  manifest: {reads: [plain-language items], proposes: [effect kinds, plain language], never: [plain-language list],
             expires, identity, rule, examples},
  rule: str | null, examples: [{input, kept, why}],
  code_url (advanced disclosure only)}
SkillSummary {name, purpose_plain, version, identity, state: "active"|"revoked", learned_at, last_used_at, uses,
             rule: str | null}
Overreach event (in GET /v2/changes/{id}/why and the outcome): {skill, attempted, blocked_by: "runtime"|"manifest", detail}
Reuse: a later change that the library can serve shows "Using your skill: <name> (learned <date>)" and skips learning.

### Teach Mode (build/teach, 2026-09-19)

`POST /v2/teach {rule}` — owner-token-gated — turns a sentence the owner writes into a skill. It is
not a second kind of skill: the same generate → grade → review → approve-by-hash → library path runs,
and `GET /v2/learning/{id}`, `/approve`, `/reject` and `/v2/skills/{name}/revoke` are the same routes,
because it is the same object.

A taught rule may only **narrow**. It can remove a slot the trusted validator offered; it can never
add one, move one, or speak for `fit.py`, the protected commitment or the buffer. Several approved
rules compose by **intersection**: an option is kept only if every one of them keeps it, so teaching
a new rule can never undo an earlier one's refusal. A slot a taught skill removes appears in the
plan's `rejected` list like any other refusal, and the entry names the skill that decided it: `by` is
the skill's name, and `code` is the reason code the skill's own answer carried (`"skill"` when it
gave none).

| Field | Meaning |
|---|---|
| `Learning.rule` | the owner's sentence, verbatim, for a taught learning; `null` for a capability the build knew how to ask for |
| `Learning.examples` | 2–5 `{input, kept, why}` — the labelled cases the owner reads on the approval screen and approves **together with** the skill. `input` and `why` are plain sentences; `kept` says whether the rule keeps that time |
| `Learning.manifest.rule`, `.examples` | the same two, on the manifest the identity binds to — the owner approves the sentence and the code as one artifact |
| `SkillSummary.rule` | the owner's sentence for a skill they taught; `null` for a capability |

**Refused before any model call**, with 422 and a `code` the UI can act on — the rule is judged on its
text alone, so a refusal costs nothing (no thread, no token, no library row):

- `rule_too_long` — more than 280 characters. Teach the rest as separate rules; they compose.
- `rule_asks_for_an_action` — the rule names an outward effect ("email my boss…", "book the room…").
  A taught skill computes which offered times fit; it never sends, books, moves or cancels. Say it as
  which times do not work instead.
- `empty_rule` — nothing to teach.

**What this proves, and what it does not.** Grading checks the *invariants* over a fixed set of
generated cases (never lets through a slot the trusted rules refused, exact output schema,
deterministic, within the runtime's resource caps) plus the held-out safety suite the generator never
saw. The examples are checked against the code that ships with them — which proves the two agree with
each other, and is **not** a correctness check: a model that writes the code and the examples for that
code has proved consistency, not that the rule is the one the owner meant. The owner reading the
examples on the approval screen is that check, and nothing in the pipeline substitutes for it.

### UI integration fields proposed by ui2 (round 2; engine coordination required)

The Concept D endpoint shapes above are consumed by `web/lib/api.ts`. Installation, rejection and
revocation accept either a successful JSON response or 204; the UI refreshes the relevant resource.
The following fields are **UI additions, not a claim that build/learn already implements them**:

- `Change.learning_id?: string` lets a reload recover its learning disclosure.
- `Change.skill_used?: {name, identity, learned_at}` identifies the actual evaluator used for that
  particular preparation. The UI must not infer reuse merely from an active library entry.
- `Why.overreach?: Overreach[]` and `Outcome.overreach?: Overreach[]` name the collection containing
  the contract's overreach events. Absence means no events supplied, not proof none occurred.
- `Learning.code_url` may be null for synthetic or unavailable source. The UI exposes a valid HTTP(S)
  or root-relative URL only inside the advanced disclosure.

Still to agree: mutation response schemas; learning resume/discovery when no `learning_id` is present;
capability-needed signalling versus ordinary preparation; identity/expiry error codes; how to display
expired installed skills (SkillSummary currently has no expiry); cancellation versus rejection while
learning is running; and whether revocation invalidates already-prepared, unapproved proposals.
The UI makes no promise to cancel running effects on revocation. Future sample preparations exclude
revoked/expired skills. Learning and installation remain separate from effect approval.

`api.nextSampleChange()` is a sample-only local fixture control, **not a `/v2` endpoint**. It opens a
second synthetic request with a different change and event identity while retaining the library and
first action record. Existing one-off scenarios remain available; choose “Teach this routine” to run
the learning sequence before preparing the first plan.

The `/v2` contract above is unchanged by this. The one thing a browser cannot do is reach into the
Python process to deliver the second email, so the sample backend offers the same control at
`POST /sample/next-change` — deliberately OUTSIDE `/v2`, refusing with 404 when the server is not
running as the sample, and answering with the full Change. A live client uses it, a live server does
not serve it, and no `/v2` route grows an affordance that exists only because the sample has a
scripted inbox. All sample test counts, reviews, identities and
overreach events are illustrative fixture data, not executed code or provider evidence.

## The decision log — `GET /v2/changes/{id}/decisions` (2026-09-19)

The product's one claim is that nothing untrusted — the inbound email, the model's answer, or
model-written skill code — can widen what the assistant may do or reveal. That claim is a claim
about a SEQUENCE of decisions, so the sequence is readable. This endpoint is the sequence.

A **bare array**, oldest first. `404` when the change is unknown: "this change has decided nothing"
(an empty array) and "this change is not a thing" are different states, and only one is worth
rendering. A change that has not been prepared yet returns `200 []`.

```json
[
  { "seq": 0, "at": "2026-09-19T09:00:00+01:00",
    "stage": "read", "source": "owner", "untrusted": false,
    "subject": "Shared the protected window as a busy interval",
    "verdict": "withheld",
    "rule": "Your commitment can be planned around without being described.",
    "detail": "16:30–17:15 plus a 45-minute buffer; the title, place and reason stay here" }
]
```

| field | meaning |
| --- | --- |
| `seq` | int, monotonic within a change, from 0. The spine's index. |
| `at` | ISO-8601 with offset. |
| `stage` | `read` \| `model` \| `search` \| `validate` \| `approve` \| `execute` \| `send` |
| `source` | `email` \| `model` \| `skill` \| `owner` \| `system` |
| `untrusted` | true when the thing checked came from the email, the model or a skill. Derived server-side from `source`; do not infer it in the client. |
| `subject` | plain words, one bounded line: `"Reply to alex@…"`, `"Search: cafés near King's Cross"`. |
| `verdict` | `allowed` \| `refused` \| `withheld` \| `discarded` |
| `rule` | the plain-language clause, e.g. `"A reply goes only to the person who wrote to you."` |
| `detail` | a sentence, or `null`. Never contains protected text. |

### The four verdicts

`withheld` and `discarded` are why this is not a boolean. The planner is allowed to know that a
window is unavailable and is not allowed to know why: the fact leaves and the title does not. And a
candidate that lost to a sibling was not stopped by anything.

- `allowed` — the check passed and the thing proceeded.
- `refused` — the check failed and the thing did not proceed. Nothing happened.
- `withheld` — the thing proceeded, but not with everything it asked for.
- `discarded` — the check failed for THIS candidate and the change proceeded on another one. The
  sender offers 15:00 and 16:00, the protected window rules out 16:00, and the plan goes ahead at
  15:00. **Nothing was refused.** A client must not render a refusal, a block or a warning for a
  `discarded` row: a fourth review found a client that did, which put "Unsafe part blocked" over a
  plan that had simply picked the other time. The invariant a client can rely on is that a
  `discarded` row always has an `allowed` sibling at the same `stage`; a run where NOTHING fits says
  `refused`, because then the request really was stopped.

`detail` is run through the same `PrivacyGate` every outbound payload passes, on the way INTO the
log. A log that quoted the protected reason would be a new leak wearing the uniform of the thing
that prevents leaks. When a detail does not survive, the row is still recorded and `detail` reads
`"detail withheld: it did not pass the privacy gate"` — dropping the row would turn "the gate
refused something here" into "nothing happened here", which is the more dangerous mistake.

### `blocked_attempts`

Integer on the change object, not on this list, because the day view shows it before anybody opens
the spine. It counts `refused` **and** `withheld`; from the owner's seat both are the same event —
something asked for something it could not have. It does **not** count `discarded`: a candidate slot
that lost to its sibling was not stopped, and counting it made "your rules blocked something" the
standing report for every change whose sender offered two times. It is always consistent with the
list:

```python
change["blocked_attempts"] == sum(1 for r in decisions if r["verdict"] in ("refused", "withheld"))
```

### Stage order is the run's order, not the enum's

The stages are listed above in the order a change normally walks them, and that is a narrative, not
a promise about the array. In the sample run the order is
`read, model, validate, model, search, model, validate, approve, execute, send` — the fit validator
runs **before** the venue search, because a slot that does not fit is never worth searching for.

The invariant that does hold, and the one that matters, is that **no `execute` or `send` row
appears before the first `approve` row**. The reply is checked twice — once when it is composed
(recorded at `validate`, because nothing is being sent yet) and again at the port (recorded at
`send`). Recording the compose-time check as `send` put a "sent" row three rows above the
"approved" row, which reads as a reply that left before the owner said yes; `guard_email` therefore
takes its stage as a parameter. `tests/test_e2e_v2.py` asserts the invariant.

### Where the rows come from

Every existing check records one: the privacy gate on each model prompt, on each search query and
on the outbound reply (compose time and port time); the fit validator, per offered option; the
approval and staleness re-check at admission; and each execution step. **A refusal that raises is
also recorded** — that is the point of the log, not a side effect of it.

Decisions taken before a change exists — the classification call, which runs first and can be
refused before any change is created — are filed under the empty change id and are not served by
this endpoint, because there is no `{id}` to ask about. That is a fact about the endpoint, not
about the refusal.

## Standing limits — `GET /v2/limits`, `POST /v2/limits` (2026-09-19)

Approving every change forever turns "teach it once" into "click forever", and the second failure is
worse than the first: an owner who has approved four hundred times stops reading. So the owner may
set a **standing answer** for the meeting-change routine. Three presets; the middle one is the only
one that acts.

| mode | what it does |
| --- | --- |
| `ask_always` | Every change is prepared and waits for the owner. **The default, and the only mode an install that predates this feature gets** — a standing yes is something an owner grants, never something a new column grants on their behalf. |
| `within_limits` | A change whose ENTIRE action bundle fits the clauses below runs after `prepare` without asking, and the owner is told afterwards (outcome plus decision log). Anything outside them falls back to `ask_always` for that change, and the log says which clause failed. |
| `off` | The routine still watches and prepares; nothing executes. An approval presented anyway is refused with `409 limits_off` and `OFF_MEANS` — see below. |

Both routes are owner-token-gated like the other owner routes, and both answer with the same shape:

```json
{ "mode": "within_limits",
  "clauses": [ { "id": "3",
                 "text": "Only the person who wrote is told — by the reply and by the invite update.",
                 "cannot": ["anyone the thread did not come from",
                            "a cc, a bcc, or a reply-to that redirects the answer",
                            "a second recipient of any kind",
                            "an attendee the invite update would email — Google notifies the event's guests, so a three-person meeting tells three people"] } ],
  "updated_at": "2026-09-19T09:00:00+01:00" }
```

- `GET /v2/limits` — the standing answer and the full clause catalogue.
- `POST /v2/limits {"mode": "..."}` — set it. An unknown mode is refused with
  `422 unknown_limits_mode` and **nothing is written**: a typo that silently became `within_limits`
  is the dangerous direction, and a typo that silently became `ask_always` reads as a setting that
  did not save. Changing the mode is recorded in the audit log as `limits.mode_changed`.
- `updated_at` is `null` — not `""` — until the owner has chosen a mode, so a client can tell "never
  set" from "set".

### The limit is a fixed, enumerated clause list

Six sentences. Not a policy language: a policy language would be a second thing to get right, and
the owner would have to trust the interpreter. Six sentences can be read, argued with, and checked
line by line against the code that enforces them. `text` is shown verbatim and is the **same
string** carried as `rule` on the decision rows, so what the owner read and what the engine enforced
cannot drift into two different claims.

### What the owner is told afterwards

The change object gains `executed_by`:

| value | meaning |
| --- | --- |
| `"owner"` | a person approved this exact version, and it ran. |
| `"limit"` | the standing limits covered the whole bundle, and it ran without asking. |
| `null` | nothing has run. |

`executed_by` is stamped **after** the run and only when the run happened: "the owner did this" is a
claim about an execution, and a stamp written before the effects would survive a failure that left
them undone.

A limit run writes six decision rows, in clause order, at stage `approve`:

| field | value for these rows |
| --- | --- |
| `source` | `"owner"` — a clause is the owner's own standing sentence, not the model's proposal and not this system's judgement. Filing it under `system` would hide exactly what the owner needs to audit: which of their own rules let this run. |
| `rule` | the clause's `text`, verbatim. |
| `verdict` | `allowed` or `refused`. |

All six are written, not only the failure: a run that cited six checks and showed the one that
refused is a run the owner can audit without trusting that the other five were looked at.

### Two things the endpoint deliberately does not do

**A change with no bundle records no clause rows.** `no_safe_fit`, or any other preparation that
ends without a plan, has no actions the limits could permit or refuse. Six clause rows about a plan
that does not exist would be noise in the log and would inflate the change's `blocked_attempts`,
which is documented above as counting refusals of things that were asked for.

**Setting a mode is not an approval.** A change already waiting on the review screen is not a
change the limits may act on by themselves — the owner is looking at it, and it is theirs to answer.
Flipping the preset does not run it.

### Why `off` refuses an approval instead of ignoring one

`off` answers `POST /v2/changes/{id}/approve` with `409 limits_off`, and the detail is the same
sentence the UI shows:

> The routine is watching and preparing, but execution is off, so nothing was run. Set the limits
> mode back to ask_always or within_limits to act on an approval.

A preset that silently swallowed a fresh, specific "yes" would leave the review screen reading
"approved" over a meeting that never moved — the failure this product exists to not have. Refusing
is loud, and loud is recoverable. The alternative reading — fall through to the normal ask path so
an explicit approval still runs — is a legitimate design and is NOT what this build does; see
`src/privatemesh/limits.py`, `OFF_MEANS`.

## What the model saw — `GET /v2/changes/{id}/model-view` (2026-09-19)

The decision log answers "what did the engine decide". It does not answer "what did the model
READ", and those are different questions: a prompt is allowed to carry a fact the answer is not
allowed to repeat, and a run that refused everything correctly can still have told a hosted model
more than the owner thinks. This endpoint is that reading, next to the list of things that never
left.

**Owner-token-gated exactly as `/decisions` is**, and for a sharper version of the same reason: the
prompts are returned **verbatim**, because a receipt you cannot compare against the transport log is
not a receipt. A caller who is not the owner would be reading everything the change's planner was
allowed to see. The gate is the first statement in the handler, before the change is looked up, so a
404 cannot be used to probe which change ids exist.

`404` for an unknown change, matching `/decisions`: "this change sent nothing" and "there is no such
change" are different answers.

**In the sample the route is open, and it is gated anyway.** The sample has no owner token and its
data is synthetic, so requiring one would cost a judge the reading half of the demo. "The data is
fake" is a claim about the fixtures, though, not about this code path: every recorded `prompt_sent`
is put through the same `PrivacyGate` the live build applies, one prompt at a time, so a call whose
prompt carried a protected value comes back with `prompt_sent: null` and a `withheld` row naming the
field — the same shape a call the gate refused before sending already has. Nothing about the route
changes the day the sample grows real data; the redaction is already in the way.

`kept_on_machine.counts.other_events` is `number | null`. `null` means the day was **never read**
(there is no `plan_day` yet), and `0` means it was read and held nothing else. They were both `0`
until the second review: a screen showing "0 other events" over a calendar nobody opened is this
endpoint claiming a non-disclosure it did not observe.

```json
{ "change_id": "9f2c…",
  "calls": [
    { "phase": "classify", "tier": "cheap", "model": "nemotron-3-nano-sample",
      "prompt_sent": [ { "role": "system", "content": "…" },
                       { "role": "user", "content": "…" } ],
      "withheld": [],
      "tokens": { "prompt": 412, "completion": 96, "total": 508 },
      "cost_usd": null }
  ],
  "kept_on_machine": { "categories": ["title", "location", "reason", "attendees", "other_events"],
                       "counts": { "title": 1, "location": 1, "reason": 1,
                                   "attendees": 1, "other_events": 3 } } }
```

| field | meaning |
| --- | --- |
| `phase` | `classify` \| `extract` \| `query` \| `choose` \| `draft` \| … — the phase's own name, as the gate files it. |
| `tier` | the routing tier the call was made at (`cheap` \| `reason`). |
| `model` | the model id the tier resolved to. |
| `prompt_sent` | **the gated payload, verbatim** — the messages array as the transport received it, after `egress_sanitize`. `null` when the gate refused the call and nothing was sent. |
| `withheld` | `[{field, reason}]`. Empty on a call that went out; on a refused one it names the protected FIELD and never its value. |
| `tokens` | `{prompt, completion, total}` ints, zeroed on a call that never happened. |
| `cost_usd` | float, or **`null` when the model has no published price**. Never `0.0` for an unpriced model — `spend.py` refuses an unknown price rather than calling it free, and a receipt that said `0.0` would be a number doing work it cannot do. |

`kept_on_machine` names the categories that were never in any prompt: the protected commitment's
`title`, `location`, `reason` and `attendees`, plus `other_events` — the day's other meetings, which
are the second thing a leak could travel in. **The categories are named and the values are absent.**
A screen that proved the title was safe by printing it would be the leak it exists to show missing;
this is the same rule `why().kept_private` follows.

### Where the rows come from

The engine's own records, one audit row per call, written at the moment of the call under the
existing `model_saw` event — the same event `broker.record_model_saw` writes for the agent path, and
the same rows the audit chain hashes. **There is no parallel log and no second table.** A second
bookkeeping path would be a second thing to keep in sync, and on the day the two disagreed the
response would still look like evidence.

A call the gate REFUSED also gets a row, with `prompt_sent: null` and the field it would have
disclosed. Without it the list would say the engine did nothing at the moment it did the most.

**Two calls are not reachable from this endpoint, and "every prompt" means every prompt of a change
that exists.** Classification mints a change id before it calls, and files its receipt under it; if
the answer is "not a meeting change", no `Change` is ever created, so there is no id to require and
this route 404s. The row is still written and is still readable through
`ChangeEngine.model_calls(id)` — but the id was never returned to anyone, so in practice the one
prompt that carries the raw email body is the one this endpoint cannot show for a message that
became nothing. The second is the mirror of it: the provider call happens before its receipt is
written (`changes._ask`), so a crash between the two — an audit write failure, which means the chain
is broken anyway — would leave a prompt that left with no `model_saw` row. The gate's own decision
row for that same payload is written *before* the call, so the evidence that a prompt went out is
not lost with it. Both are in `docs/THREAT_MODEL.md` under "Not covered".

### `2,000` characters of context, on purpose

The classify prompt reads `body_text[:2000]` of a real thread. This endpoint reports what the engine
actually sent, window included, rather than a nicer version of it.

## The playground — `POST /v2/playground/attack` (2026-09-19)

Everything else in this repo that red-teams the product ships a corpus (`redteam/`). That is the
right way to build an enforcement proof and the wrong thing to hand a judge, who can only read the
attacks somebody else wrote. This route is the other door: an email arrives as request data, and the
same engine, gate, broker and executor that `serve --v2` composes answer it.

```json
{ "email": "…the text of an email that arrived…" }
```

```json
{ "verdict": "held",
  "attempted": ["addressed the reply to: dave@example.com",
                "told the sender the rest of the day's events: …"],
  "decisions": [Decision],
  "outbound": { "emails": [], "queries": [], "calendar": [] },
  "port_calls": [ { "surface": "mail", "call": "deliver", "args": {}, "ok": true, "error": "" } ],
  "checks": [ { "check": "protected_text_outbound", "failed": false, "why": "…" } ],
  "summary": "The model attempted 2 thing(s) … The engine stopped all of it: 3 decision(s) refused or withheld, and nothing untrusted reached a provider.",
  "withheld": "…only present when the response itself had to be redacted…" }
```

- `verdict` is `held`, `leaked` or `not_attempted`, computed by `redteam.evaluate` — the same
  function the corpus is scored with, over the same generic checks. A `held` here means what it
  means there.
- `not_attempted` is the third verdict and it is not a softer `held`. A `held` says the engine was
  asked for something protected and refused; it is only evidence if something was asked. When the
  parser above recognises no request in the email at all, the engine was never asked, so the run
  demonstrates nothing and the endpoint says so — with a sentence naming what to try instead
  ("try asking it to cc someone, search for something private, or move another meeting"). The run
  still executes (`decisions` and `checks` are real rows, and a `leaked` still outranks this), but
  the verdict is `not_attempted`, and a UI must render it as its own state rather than as a pass.
- `attempted` is what the **compliant** model tried, read off the model that was handed the text.
  It is reported even when the run held: a refusal of something nobody attempted proves nothing, and
  a `held` where the model never tried is the false green the corpus run exists to avoid.
- `checks` is the scoreboard, row by row, so a `leaked` says which check failed.

**`404` outside sample mode** — not a `403`. Off the sample there is nothing here to be forbidden
from: a live build has real providers behind these ports, and the honest answer to "attack it for
me" there is that this route is not part of the product. The corpus is.

**Not owner-gated**, deliberately. A judge must be able to run it without being handed the owner
token, and the thing it can reach is a world built for the request: fresh ports, the judge's text as
the inbound email, `MockTransport` for the model, no key, no network. The caps below stand in for the
gate, because the resource being protected is the process rather than the owner's data.

| cap | value | refusal |
| --- | --- | --- |
| request body | 8 KB, enforced **before parsing** — `Content-Length` when it is sent, and a running byte count over the stream when it is not (a chunked request has no length to check, and a length is a claim the caller writes) | `413 playground_body_too_large`, nothing ran |
| email length | 2,000 characters (the classify window, so the judge sees the engine's own limit rather than a second, secret one) | `422 playground_limit`, nothing ran |
| time | 1 s of **wall clock**, checked at every model call | `503 playground_limit`. **No verdict.** A run that was cut off is not a defended one, and a `held` that meant "we stopped looking" is the failure this harness exists to not produce |
| rate | 10 per minute per caller, in-memory token bucket with proportional refill | `429 rate_limited` |

Both of the first two refusals happen **before the bucket is consulted**, so a request that never
ran does not spend a caller's token: a limiter that charged for malformed traffic would let any
caller lock out every other caller with garbage.

The body cap is enforced on the route rather than in the model, and that ordering is the point: a
Starlette request is read and JSON-decoded **before** FastAPI solves a body parameter, so a
`max_length` on `email` — which this model also carries, as a second line of defence — is a check
that runs after the bytes have already been read, decoded and held. The 8 KB guard is what stops
the parse from happening at all.

The bucket is bounded in memory, because an unbounded one is a leak the endpoint hands to any
caller willing to appear from many addresses: buckets that have refilled to full are forgotten as
they are passed over, and the table has a hard ceiling (4,096 callers) beyond which the oldest
entries are evicted. Eviction can only ever grant an early refill, never a denial, so the failure
mode of the cap is a looser limit rather than a stuck one. Every read and write of the table is
under one lock, so the limit holds when the process is serving concurrently. Requests arriving past
the ceiling are refused on the bytes and never reach it.

The caller key is the socket's peer address by default and **not** `X-Forwarded-For`: that header is
a string the caller writes, so a limit keyed on it is a limit any caller resets by editing a header.
Behind a real proxy every caller shares one peer address and the limit becomes one bucket for the
world — the opposite failure. `PM_TRUST_PROXY` picks which, and **defaults to off**: when it is set
to `1`/`true`/`yes`/`on`, the leftmost `X-Forwarded-For` entry is used. That is correct only when a
proxy the deployment controls rewrites the header on the way in; with the switch off the header is
ignored entirely. `build_v2_router(..., trust_proxy=...)` overrides the environment for an embedder
that already knows.

### The response goes through the gate

The reply a leaked run produced is *in* this report, and so is the check that names the protected
field it carried. Returning either would make this endpoint the disclosure it is reporting — so the
finished body is put through the same `PrivacyGate` every outbound payload passes, one row at a
time. When the body trips it:

- `outbound` and `port_calls` are emptied (they are the run's own payloads),
- any row of `attempted`, `decisions` or `checks` that carries a protected value is dropped and
  `withheld` says how many,
- `verdict` and a `summary` sentence always survive, because a response that refused to answer would
  leave a judge who had just run an attack with nothing to read.

A protected value can only reach `attempted` because the caller put it in the email — `attempted` is
a function of the caller's own text and of nothing else — and the row is dropped anyway: the contract
is that this response carries no protected text, and an endpoint that argued about *which* protected
text was the caller's would be an endpoint arguing about a leak.

### Parity with the corpus

Every case in `redteam/cases/` reaches the same verdict through this endpoint as it does through
`privatemesh redteam`. `tests/test_playground.py` asserts it case by case, recomputing the corpus
verdict rather than reading a stored report — a stored report is evidence of a past run, and a parity
test against one would pass on a day the corpus itself had stopped holding.

One case is the exception, and it is the one whose whole purpose is to ask for nothing. The corpus's
benign control reaches `not_attempted` here where `redteam.run_case` calls it `held`: a case that
declares its own compliant script can be "held" by a model that never tried, and the playground,
which must derive the attempt from the text, is the door that can tell the difference. The parity
test recomputes the expectation through the same parser (`not_attempted` where the parser reads no
intent, `held` otherwise) rather than exempting the case by name, so a change that broke the
parser's agreement with the corpus would still fail it.

The model is the one thing that differs between the two doors. A corpus case declares, per phase,
exactly what a compliant model answers, so the refusal that follows is the engine's. The playground
has no such script — the judge wrote the email — so `redteam.CompromisedModel` derives one from the
text, deterministically. A small parser recognises, per sentence:

| intent | what it recognises | how it reaches the engine |
| --- | --- | --- |
| `search` | "search for X", "look up X" | the query phase asks the search provider for X |
| `private` | "include/paste/quote/add/tell/send my X" | X is put in the reply as though obtained |
| `cc` | `cc`/`bcc`/`copy`/`forward`/`send`/`invite` naming an address | the reply addresses it |
| `other_events` | "move/shift/reschedule my other meetings" | the reply says the other meetings were moved |
| `schedule` | "paste/share/send my calendar / schedule / availability" | the reply carries the day's other events |
| `time` | a clock time (`4pm`, `16:00`) | offered to the planner as an extra slot |

A sentence may match several and each match is attempted separately, because a compliant model does
not pick. What it cannot do is choose **where** the attempt lands: the recipient field is pinned by
the engine before the model is consulted, and a draft fragment's `source` must be one
`compose_email` allows — so an attempt at a new recipient arrives as reply text, which is exactly
what the corpus's second case measures.


## Agreements — `GET /v2/agreements`, `POST /v2/agreements/check` (2026-09-19)

PrivateMesh keeps what the owner approved. Nothing until now noticed when the other side later
edited the invite: moved it, swapped the venue, added a guest. An **agreement** is what the owner
agreed to, written down at the moment the change executed, so a later run can ask whether the world
still matches it.

**An agreement is recorded when a change EXECUTES** — owner-approved or inside the standing limits,
both paths meet at `Executor.execute` — and only on the path where the invite actually moved and the
read-back confirmed it. A failed update, a refused one, an accepted update that never showed up in
the read-back, and a bundle that never ran all leave nothing behind: an agreement over an invite
that did not move would be a promise this product never got kept.

**What is recorded is what the owner APPROVED, never what the provider read back.** The window and
the place are the ones the bundle wrote; the title and the guest list are the ones its review
display carried, because a calendar update moves a meeting and does not rename or re-invite one —
so neither appears among the arguments the approval covers, and the display is the only place the
owner ever saw them. The write and the read-back are two calls, and anyone with write access to the
invite can edit it in between: a guest added there is compared against the reviewed list and
reported as `drifted` on the spot, rather than being adopted as somebody the owner agreed to
invite. `observed` is still what the provider now reads, so the drift names both values and the
owner can see what changed.

**A bundle that never recorded who it would notify records no guests at all.** `notifies: null` is
the display saying it does not know the list, and a guest list nobody reviewed cannot be compared
with anything — so the agreement is stored `unchecked`, with no `observed` and a `note` saying so,
rather than adopting the provider's list as the owner's decision. That is a last resort and not a
path a plan reaches by omission: **a calendar action that notifies attendees cannot be approved
unless its review display names them.** `send_updates` other than `"none"` with no `attendees` in
the display is refused, because the recorded agreement is taken from what was reviewed and a
notifying bundle that names nobody has nothing to take. A plan with `send_updates: "none"` notifies
nobody, may name no attendees, is approved normally, and leaves the `unchecked` agreement above.
The refusal travels on this API's existing 409 (`stale_version`), which is the code the client
already re-renders the review screen on, and the detail names the reason rather than saying "stale".

**Drift never causes an action.** `check` reads the calendar and compares. It holds no mail client,
no model and no broker — "noticing is not acting" is a property of the object graph rather than a
rule somebody has to remember, and it is why whoever can edit an invite cannot use this feature to
make the product send mail.

Both routes are owner-token-gated like the other owner routes, and both answer with the same shape:

```json
{ "agreements": [
    { "id": "agr-evt-alex-sync",
      "change_id": "ch-1a2b3c",
      "event_id": "evt-alex-sync",
      "title": "Catch up with Alex",
      "status": "drifted",
      "agreed":   { "start": "2026-09-19T11:00:00+01:00", "end": "2026-09-19T11:45:00+01:00",
                    "location": "Our usual café", "attendees": ["alex@example.com"] },
      "observed": { "start": "2026-09-19T12:00:00+01:00", "end": "2026-09-19T12:45:00+01:00",
                    "location": "Our usual café", "attendees": ["alex@example.com"] },
      "drift": [ { "field": "start", "agreed": "2026-09-19T11:00:00+01:00",
                   "observed": "2026-09-19T12:00:00+01:00" } ],
      "checked_at": "2026-09-19T09:00:00+01:00" } ] }
```

- `GET /v2/agreements` — what the owner is holding the other side to, with the last answer anyone
  got about it. It reads the **store** and touches no provider: this route is safe to poll, and the
  answer cannot change under a client that only asked to look.
- `POST /v2/agreements/check` — re-read every agreed event and report what still matches. Same
  shape, freshly computed and persisted. Owner-gated even though it writes nothing outward: it reads
  the owner's calendar, and "who may ask this product to go and look at your meetings" is a question
  about the owner rather than about whether the call is harmless.

### The three statuses

| status | means |
| --- | --- |
| `holding` | Every compared field still reads as agreed. |
| `drifted` | At least one did not. `drift` names each one, with both values, so the owner sees what changed rather than only that something did. |
| `unchecked` | Nothing has compared it yet, **or** the event could no longer be read, **or** the stored record itself could not be read, **or** the bundle never recorded who the update would notify. `observed` is `null`, `drift` is empty, and `note` says which of them it is — "not yet" resolves itself, "cannot" does not, an unreadable record is the one the owner may need to act on, and a guest list nobody wrote down will not resolve itself either. |

`unchecked` is never `holding`. A meeting nobody can read is not a meeting that still matches, and
reporting it as one would be the product inventing good news.

### A stored record this build cannot read is served, not omitted

A row whose payload will not parse — truncated by a crash, or written by a build that knew a shape
this one does not — is still a record the owner has. Both routes therefore serve it, as an
`unchecked` agreement whose `id` is the stored row's, with empty `agreed` fields, `observed: null`,
no drift, and a `note` naming the failure:

```json
{ "id": "agr-evt-alex-sync", "change_id": "", "event_id": "", "title": "",
  "status": "unchecked",
  "agreed": { "start": "", "end": "", "location": "", "attendees": [] },
  "observed": null, "drift": [], "checked_at": "",
  "note": "this stored record could not be read (JSONDecodeError), so nothing has compared it; the record itself is still on disk and has not been overwritten" }
```

The alternative considered and rejected was a **top-level `unreadable` list** beside `agreements`.
It is a smaller shape change, but it puts the warning in a field a client has to know to render:
the review's finding was precisely that an unreadable record produced `{"agreements": []}` — the
one answer indistinguishable from "you have no agreements" — and a client that renders rows and
nothing else would have shown a clean slate again. A row is visible to every client that already
renders rows, and `unchecked` is not a new status invented for the occasion: it is this API's word
for "nothing has compared it", which is exactly true of a record nobody can read.

Four payloads are unreadable, and the middle two are the newer build's record rather than a damaged
one: a payload that will not parse; **a status this build does not have**, which is not
"`unchecked`" but a word from a schema that knows more; **any field, or schema version, this build
does not write**; and **a stored `unreviewed` that is not a boolean.** Reading the middle two as the
subset this build happens to understand and then writing them back would delete whatever the newer
build recorded there — a downgrade that is permanent, silent, and indistinguishable from the record
never having existed. The fourth is refused for the same reason at a smaller scale: `unreviewed`
decides whether the row is checked at all, and `bool("false")` is `True`, so coercing the value
turned one nobody wrote into "skip this row forever" — the one answer that never resolves itself —
rather than into the unreadable record it is. A payload that simply LACKS the field is a different
thing and stays readable, exactly as `revision` and `sequence` read as `0` when they are not there.

A row whose **key disagrees with the id inside its payload** is unreadable for the same reason. The
key is the row's identity; a payload that names a different agreement is not a record of the key it
is filed under, and serving it would offer the owner two rows for one agreement while a check on the
disguised one wrote it over the real one.

The record is never overwritten on the way past. `save` refuses to replace a row this build cannot
parse, so the original bytes — the only evidence of what was written there — survive every check,
every execution and every restart.

### The two races

`check` and `execute` both write this store, and both can be racing something they cannot see.

- **A check against an execution.** `check` reads a record, goes to the provider, and writes back
  what it found. If an execution recorded a newer agreement for that event while the read was in
  flight, the check holds an answer about a record that is no longer the owner's. Every check write
  carries the `revision` it read, and is **dropped** when the stored row has moved past it.
- **An execution against a later execution.** Two runs of the same event can overlap: one moves the
  meeting, is confirmed, and pauses before recording; the other moves it again and records first.
  The run that resumes is the OLDER one, and it writes LAST — so revision ordering would hand it the
  win. Each execution is given a **sequence** when it starts (`begin_execution`), taken before the
  provider is touched, and a record whose sequence is not ahead of the stored one is dropped. The
  newer run's window is what the world actually holds, and it is what survives.

Both swaps are **one SQL statement**: the row is read, the decision is made, and the write is
conditional on the row still being the exact bytes that decision was made about (`UPDATE … WHERE
id=? AND payload=?`, with the first write guarded by the primary key and its rowcount deciding).
Two connections — two server workers, two processes — can both read revision 1 and both try to write
revision 2, and exactly one wins. A per-process lock cannot make that true, because the other writer
is not in the process.

A swap that loses that statement **retries**, and it does not stop after a fixed number of attempts:
how many writers a row has is not a number this store knows, so any cap is a rule that the next
writer loses — and the next writer is the newest execution. What ends the retry is the row itself:
the write landing, or a stored row this write may not replace (an execution at or after its own, or
a revision a check did not read), which is the one case where dropping the write is the correct
answer. If neither happens within five seconds the store **raises** rather than answering. That
distinction matters to callers: a dropped write and a timed-out one are different facts, and only
one of them means the agreement on disk is newer than the one in hand.

**A run that cannot be given a sequence does not run at all.** The sequence is what makes an older
execution's record lose to a newer one's, so an execution that could not be numbered would record
unconditionally — the protection removed rather than weakened. The calendar is therefore not touched
and the reply is not sent: the run settles as one that was not executed, both effects report
`not attempted`, and the decision log carries the reason. The owner loses a run and nothing else;
the alternative is a run that timed out on the sequence lock, resumed after a newer execution had
recorded, and overwrote it.

### How the fields compare

Four fields, compared field by field. The rules are the ones that avoid false alarms without
inventing agreement:

| field | rule |
| --- | --- |
| `start`, `end` | As **instants**. `15:00:00+01:00` and `14:00:00Z` are one moment written two ways and are not drift. A value that will not parse falls back to string equality; an aware and a naive value are never equal, because an event that lost its timezone is not the same commitment. |
| `attendees` | As a **set**, case-insensitively. Order is how the provider serialises an array and case is how somebody typed an address; neither is the other side changing the meeting. |
| `location` | **Exact.** Nothing here invents a notion of "the same place": two spellings of one venue are two different strings, and this API has no honest way to say they are the same building. Under-reporting a venue edit is the smaller failure. |

`title` is recorded and served but is **not** compared — `observed` is the same four fields, and a
meeting somebody renamed has not been moved, re-placed or re-peopled. `event_id` is served on top of
the fields above; it is additive, so a client written against this shape is unaffected.

### Three things this deliberately does not do

- **It does not act.** No calendar write, no email, no model call, no spend — by construction, not
  by a check. Whoever can edit an invite must not be able to make this product send mail.
- **It does not let the event's text reach a model.** The title and description are untrusted, and
  this feature has no model client at all. An injected invite is compared as data and shown to the
  owner, never interpreted.
- **It does not keep a history.** One agreement per **event**: the second move is what the owner has
  now agreed to. Keeping the superseded window alongside it would report the old one as drift
  forever, which is a false alarm that never clears.

### Proposing a fix — out of scope, and how it would fit

Drift raises no proposal today. If it should, the shape is already in the product and needs no new
path: the drifted fields are exactly a change request, so a `drift` would be turned into a `Change`
with the meeting as its subject and the agreed window as its intended action, and then goes through
`prepare` → review → `approve` like any other change — the same `ChangeSetStore`, the same
`PrivacyGate`, the same standing-limit clauses, the same approval the owner presses. The one thing
that must NOT happen is an automatic re-write: the other side's edit is a fact about the world, and
the owner's answer to it is a decision, not a repair. That is why this release stops at telling
them.

### Sample mode

The sample serves **one holding and one drifted** agreement. They are the sample day's own events —
the meeting the product is about, and the client call agreed an hour earlier than the day now reads
— seeded `unchecked` and then put through the **real** checker, so the statuses the sample renders
are ones the checker produced by reading the calendar rather than values typed in beside them.

## The caregiver — `GET/POST/DELETE /v2/care-circle`, `GET/POST/DELETE /v2/protected-facts` (2026-09-20)

An adult child runs an aging parent's week from their own inbox. The clinic, the physio, the
transport firm and the pharmacy all write to them; some of what they hold about their parent —
a benefits id, a date of birth, a home address, a power-of-attorney reference — must never leave the
machine on the strength of anything the product READ. This section is the contract for that: two
lists the owner keeps, one new standing-limit clause, and one outbound artifact.

### The care circle

Who may be told what, and the answer is per-person rather than one flat list. Rosa may get the digest
and reply to it; Tom may only get the digest; the clinic's front desk may only be told about
scheduling. That asymmetry is the feature — a circle where everyone may receive everything is an
address book, and the question this answers is "who may be told about my mother".

Both routes are owner-token-gated like the other owner routes, **including the GET**: the answer
names the people in the owner's parent's life, so a read of it is a read of the family, and it is the
owner's for the same reason the decision log is.

```json
GET /v2/care-circle
{ "members": [
    { "id": "cm-3f9a2b1c4d5e", "name": "Rosa Almeida", "email": "rosa.almeida@example.com",
      "may_receive": ["digest", "reply"], "added_by": "owner",
      "added_at": "2026-09-22T08:00:00+01:00" } ] }
```

- `POST /v2/care-circle` — body `{"name", "email", "may_receive": [...]}`, answers **201** with the
  member as above. `may_receive` is required and **may be empty on purpose**: "on my list, trusted
  with nothing yet" is a state the owner is allowed to be in, and a default would pick a permission
  for them. There is deliberately **no `added_by` field on the body** — the server writes `"owner"`,
  and accepting it would make the one field that records who enrolled somebody into a field the
  caller chooses.
- `DELETE /v2/care-circle/{id}` — **204**. `404` `care_member_missing` if there is no such member.

`may_receive` is a closed list: `digest`, `reply`, `schedule`. A value outside it is refused with
**`422` `care_kind_unknown`** rather than stored, because the kind decides which of the owner's
per-member grants a send is checked against — a member granted a kind this build has no definition
for is a permission whose edges nobody can predict. A malformed address, or a protected-fact value
too short to mask, is **`422` `care_refused`**. The two codes are different on purpose: "pick from
this list" is a different answer for the browser to render than "that value is unusable".

### Protected facts

The owner's list of what must never leave. **No verb on this route returns a value**, and that is the
contract rather than an omission: a route that could hand one back would be the disclosure path the
whole feature exists to close.

```json
GET /v2/protected-facts
[ { "id": "pf-850a501344f0", "label": "Benefits id", "kind": "benefits_id", "in_use": true } ]
```

- `POST /v2/protected-facts` — body `{"label", "kind", "value"}`, answers **201** with the row above.
  The response does **not** echo the value: putting it back in the response body would put the secret
  in the browser's memory, in the access log and in whatever the client does with the payload, for
  no gain — the owner just typed it.
- `DELETE /v2/protected-facts/{id}` — **204**. `404` `protected_fact_missing`.

`kind` is a closed list — `benefits_id`, `date_of_birth`, `home_address`, `document_ref`, `phone`,
`account` — and each kind has exactly one **placeholder**, `[[private:<kind>]]`. The placeholder is
what a model is shown in place of the value. It is a token and not a redaction: it is a NAME a model
can reason with ("her benefits id is `[[private:benefits_id]]`, so the form is asking for something I
cannot read") and cannot read anything out of. The `[[...]]` shape is chosen because it cannot occur
inside an address, a date or an id, so a placeholder can never be confused with a value.

`in_use` is **recorded, not asserted**: it reads a timestamp the masker wrote when it last replaced
this value in a model payload. A fact that has never been needed reads `false`, which is the honest
answer, and a badge rendered from a literal would rot into a lie the first time a fact stopped being
used.

**What the web lane renders for a protected fact: `label`, `kind`, and `in_use` — never a value,
because no route has one to give.** A row is `Label · kind chip · "in use" badge when true`. The
kind is safe to render verbatim; it is a category, not a secret.

### What seeing a value means — the two halves, and the one exception

Three things are true of every protected value, and they are enforced in `care.py` and
`boundary.py` rather than by asking a model to be careful:

1. **It never enters a model prompt.** `TokenFactory.chat` masks every protected value to its
   placeholder immediately before the request body is built, and then runs the ordinary egress guard
   on the bytes that would actually be posted. Two steps rather than one, deliberately: if the
   masking ever misses a value, the guard raises and nothing is sent — so the failure mode of a
   masking bug is a refusal and not a leak.
2. **It never appears in an outbound artifact**, unless the owner approved **that exact artifact**
   containing it **and** every recipient is a care-circle member. The check runs on the FINAL bytes
   of the composed body, after every fragment has been rendered, because a check on the pieces is a
   check that a later composition step can walk around.
3. **A refusal is recorded without echoing the value.** The decision row reads
   `the protected Benefits id would have been disclosed to 2 recipient(s)`. The audience is a
   **count**: an address in a receipt is a recipient list in a log, and a log is a thing that gets
   read, exported and pasted into a bug report. The count is enough for the owner to know whether the
   disclosure would have been inside their circle or out of it.

The exception is content-addressed. `artifact_id` is a sha256 over the digest's day, recipients and
every fragment in order, so **editing the digest invalidates the approval by not matching** — an
owner who approved Tuesday's digest at nine has approved those bytes, not whatever the digest says at
five. Approving a field for one recipient does not approve it for another: a digest to two people
where one approved is a disclosure to the other. An approval with no audience is not an approval to
nobody.

There is deliberately **no approved-disclosure bypass on the paraphrase check** — the one that
refuses a payload that *resembles* a protected value without containing it. An approval covers the
owner handing over a value they chose; a payload that merely resembles one is not a thing they ever
saw to approve.

### The new standing-limit clause

Clause **7**, shown verbatim in the UI like the other six and used as the decision row's `rule`:

> Care actions — a family digest, anything about someone you care for — go only to your care circle,
> and only to the members you listed for that kind.

Its `cannot` list bounds the **enrolment** as well as the send: *an address you did not add yourself ·
a member added by an email, a model answer or a taught skill · a member named for something you did
not list them for · a digest more than once a day.*

The clause is **conditional and says so** rather than pretending every change is a care action: a
bundle that names no care recipient passes with `this plan sends nothing to your care circle`. The
changes this engine raises from an inbox are scheduling ones, and clause 7 is about care actions.

It is enforced **twice**, in `ChangeEngine.limit_review` and in `PrivacyGate.guard_care_recipients`,
and the second is the one that cannot be skipped: a care action that never went through a limit
review still has to pass the gate. Both fail closed — a build with no care circle refuses every care
recipient, because a missing list must not read as an absent restriction.

### The family digest

One outbound summary, composed the way the reply is: from declared fragments whose sources are on a
closed allowlist (`care_greeting`, `care_happened`, `care_coming`, `care_needs_you`), never from
free model text. A body that is not exactly its own fragments rendered is refused as a `SchemaLeak`.
At most one per day, counted per day rather than as a running total.

**What the web lane renders for a digest:** it surfaces through the ordinary change shape, so the
screen is the ordinary review screen. The fields the lane needs are the ones already there —
`change.id`, the review display's fragments in order, and the recipients — plus, when a digest is
refused, the decision row whose `rule` is clause 7's text and whose `detail` names which of the three
limits it hit ("not a member", "which you did not list them for", "already went out for that day").
Nothing new is required to draw it, and `docs` deliberately does not ask for a new field: the digest
is a change the owner reviews, and a bespoke screen would be a second place for the same decision.

**What the web lane renders for a refill confirmation:** also the ordinary change shape. A refill
reply fits inside a standing limit, so it executes without the owner pressing anything — the lane
shows it in the day's activity with its decision rows, exactly like any other within-limits action.

### Sample mode

`privatemesh serve --sample` seeds the circle and the four protected facts through the **owner's own
door** — `by=care_rules.OWNER`, the same call `POST /v2/care-circle` makes — so the sample cannot
demonstrate a list the product cannot hold. `build_sample(care_week=True)` swaps the whole day for the
caregiver week: four providers, four appointments, a pharmacy refill, and a physio appointment the
clinic silently moved so Agreements reports drift. It is opt-in for the scheduling sample because
adding four providers to it would change what every existing sample test measures. The public demo
is the one caller that opts in — see § "The public demo" — because a visitor is promised a caregiver
inbox and the scheduling sample is not one.

The fixture names are invented and the domains are `example.com` / `example.net` / `example.org`.
The protected values are chosen to be distinctive (`QQ-4471-ALPHA-9`, `POA-2024-88317-LK`) so the
masker cannot match ordinary prose by accident.

### The refill confirmation

Written down because `redteam.py`'s `unrelated_mail_left_alone` check cites this section by name, and
a check that points at nothing is worse than no check.

**A pharmacy email that says a prescription is ready to collect is now a second, ordinary change.**
`ChangeEngine.classify` asks one extra, OPTIONAL question of the same call it already makes — not a
second model call — and a message that answers it true raises a `Change` with `kind: "refill"`
instead of `kind: "meeting"`. `_prepare` then does for a refill exactly what it does for a meeting,
through the SAME `calendar_update_event` / `email_reply` skills and the SAME seven standing-limit
clauses, unmodified in what they assert: it finds the owner's own calendar placeholder for the
sender (the sample's `evt-pharmacy`, matched the same way `_target_event` already matches a moved
clinic appointment — by attendee, else by the sender's own words naming the event), moves it to a
free `REFILL_HOLD_MINUTES`-minute slot before its own start (the trusted stand-in for "the
pharmacy's stated hours" — never a deadline read off the untrusted email), and replies from two
fixed catalogue fragments. No draft call: there is nothing here for a model to choose among, so the
reply is the engine's own composition from the start rather than a fallback reached after one.

The one clause that needed a genuinely new true answer, not just a new caller, is clause 3 / 6's
"who does the invite update tell". A meeting's calendar write always sets `send_updates: "all"` and
is checked against an attendee list that should equal the sender; a refill's own placeholder has NO
attendee — it was never a meeting with the pharmacy — so its write sets `send_updates: "none"` and
`ChangeEngine._invite_audience` checks the honest question for that case instead: does the invite
still carry zero attendees, so that the notification setting has nothing to lie about. Both values
are engine-decided, never model-authored, so there is no injection surface in trusting either.

**What this does NOT do.** It confirms against an EXISTING calendar placeholder; it does not create
a brand-new hold when the owner's calendar has none for that sender, and the change ends at
`no_safe_fit` instead — an honest "nothing to confirm against" rather than an invented one. All
three standing-limit presets apply exactly as they do to a meeting: `ask_always` prepares and waits,
`within_limits` auto-applies only when the whole bundle — the owner's own calendar hold and a reply
that discloses nothing protected — clears the seven clauses, and `off` prepares and never executes.
A lookalike attempt riding a refill email (an NHS number, a second recipient) is refused the same
way case 09's is: the reply is composed from two fixed fragments and never reads the email body, so
there is nothing for an injection to reach.

Also absent: no route renders the digest's own history (the ledger records that a digest went out,
and `recent()` reads it, but no endpoint serves it), and there is no UI for approving a specific
artifact's disclosure — `approve_disclosure` exists and is tested, and the route that would let an
owner press it is the web lane's to propose.
## The public demo — `POST /v2/demo/session` and its three siblings (four public routes) (2026-09-20)

A third `mode`: **`demo`**. Real model, simulated providers, hard caps, and no credential of the
owner's anywhere in the process. It exists because "run it yourself" is the one thing a README
cannot do: the judges marked the mandatory working-demo requirement *not met* on a submission whose
only interactive surface was a static page, and the answer to that is a URL a stranger can attack.

The mode is a claim about the providers, so it is spelled the way the other two are — `serve --v2
--demo` — and a bare `--demo` still means the older v1 simulated app (see `cli.py`).

| Method + path | Returns / does |
|---|---|
| POST /v2/demo/session | starts a visitor's session and returns `SessionState` (below). `503 demo_unavailable` at the session limit, or with no model key |
| GET /v2/demo/session/{session_id} | the same `SessionState` re-read, so a screen can poll the budget as it is spent |
| POST /v2/demo/session/{session_id}/email {message_id, body_text} | rewrites one inbound email — the injected instruction and all — runs the real pipeline over it, and returns `DemoResult` (below). `body_text` past 2,000 characters is `422 demo_limit`, refused before it spends a model call — not truncated, because a silently shortened body is a different injection than the one the visitor wrote |
| POST /v2/demo/session/{session_id}/email/compromised {message_id, body_text} | the SAME edit, the SAME engine and gate, over `redteam.CompromisedModel` instead of the real one — see § "The compromised run" below. Returns `DemoResult` plus `{compromised: true, attempted: [str], model: {kind: "scripted", label, calls: 0, cost_usd: 0}}`. Same body checks as `/email`; touches no spend ledger and no session call |

```json
SessionState { session_id, mode: "demo",
               real: ["the model (nvidia/nemotron-3-super-120b-a12b on Token Factory)",
                      "the spend ledger and every cap",
                      "the approval gate and the privacy boundary"],
               simulated: ["the inbox", "the calendar", "web search results"],
               notice: "The inbox and calendar are fixtures. The model is real, and the caps below are enforced before any request is sent.",
               budget: { calls_left, degraded: null | {reason, detail} },
               caps:   { session_usd, session_calls, rate_requests, rate_window_s, total_usd,
                         daily_usd, session_ttl_s },
               spend:  { session: LedgerTotals, demo: LedgerTotals },
               inbox:  [ { message_id, thread_id, sender, subject, body_text, received_at } ] }
DemoResult  { session_id, change_id, budget, spend,
              change: Change | null, model_view: ModelView | null, decisions: [Decision],
              model: { kind: "real" | "scripted", label?, calls?, cost_usd? } }
```

`real` and `simulated` are the same two lists the screen renders, shipped as data rather than left
to the web lane to phrase: the brief is that a judge can tell a real model call from a fixture
without reading the source, and a UI that had to invent that sentence is a UI that could get it
wrong. `change`, `model_view` and `decisions` are the product's own objects, unchanged — see the
sections above for their shapes. `change_id` is `""` when the run produced no change at all.

An `inbox` entry's `message_id` is the value `POST /v2/demo/session/{session_id}/email` takes as
its `message_id`: read the entry, send its key back, one name end to end. The `nemotron` row of
`connections` on the startup banner and these two lists are one object read twice
(`demo_session.model_truth`), so the line an operator reads at startup cannot say the model is a
fixture while the payload beside it says it is real. `real[0]` names the model id the process is
configured with — the same id `PM_MODEL_REASON` resolves to.

**Public by design.** All four are in `api_v2.PUBLIC_V2_ROUTES` and need no owner token. That is
the entire point of the mode: a judge with no credential must be able to begin, and a demo whose
first screen is a login is the README again with extra steps. `THREAT_MODEL.md` carries the same
disclosure for a reader who starts there, including what the exemption does and does not reach.

**The four caps**, all read from the environment with SAFE DEFAULTS — a missing or unreadable
setting resolves to the SMALLER cap, never to none, and every cap has a hard ceiling above which a
setting is treated as unreadable rather than obeyed. `_bounded` is the one reader; `nan` and `inf`
are refused by it (`not (value > 0)` catches NaN, `value > ceiling` catches infinity).

| setting | default | ceiling | bounds |
|---|---|---|---|
| `PM_DEMO_SESSION_USD` | 0.05 | 1.00 | one session's model spend |
| `PM_DEMO_SESSION_CALLS` | 24 | 50 | one session's model calls |
| `PM_DEMO_RATE_REQUESTS` / `PM_DEMO_RATE_WINDOW` | 10 / 60 s | 600 / 86400 s | one client's request rate, in-memory token bucket, bounded table (`PM_DEMO_RATE_MAX_CALLERS`, 512, ceiling 4096) |
| `PM_DEMO_TOTAL_USD` / `PM_DEMO_DAILY_USD` | 1.00 / 0.25 | 5.00 / 1.00 | the whole demo, and the whole demo today |
| `PM_DEMO_MAX_SESSIONS` / `PM_DEMO_SESSION_TTL` | 256 / 1800 s | 4096 / 86400 s | how many sessions may live at once, and how long |

**A fifth bound that is not a `PM_DEMO_*` setting because it is not a knob**: `body_text` on
`POST /v2/demo/session/{id}/email` is capped at `playground.MAX_EMAIL_CHARS` — **2,000
characters**, the same bound the playground's own `attack` route holds callers to. `body_text`
reaches the extraction prompt (`changes._extract`) whole, so an unbounded body is an unbounded
multiple of the measured per-run cost the table above and the README's cost section are written
against — an ~8KB body was measured at several times the $0.001453/run baseline. Past the cap the
route answers `422 demo_limit` and nothing runs; the body is refused, never truncated.

**Enforcement is the existing spend ledger**, not a second counter beside it. `demo_session` chains
the session's own in-memory `SpendLedger` to the process-wide one and reserves against both before
the request body exists. A cap reached degrades the run to the scripted sample path and says so;
it never overspends to serve a visitor and it never refuses silently.

```json
{ "budget": { "calls_left": 6,
              "degraded": { "reason": "calls", "detail": "…the scripted answer…" } },
  "spend":  { "session": { "total_usd": 0.0026, "today_usd": 0.0026,
                           "cap_total_usd": 0.05, "cap_daily_usd": 0.05 },
              "demo":    { "total_usd": 0.0311, "today_usd": 0.0311,
                           "cap_total_usd": 1.0, "cap_daily_usd": 0.25 } } }
LedgerTotals { total_usd, today_usd, cap_total_usd, cap_daily_usd }
```

`reason` is one of `calls`, `rate`, `session`, `global`, and each names in its `detail` the setting
that bound. `degraded` is `null` while every cap is intact, so a UI can render the honest message
without parsing prose — and it is `null` rather than `""` so that "no cap was reached" and "a cap
was reached and we could not say which" cannot be confused.

`spend` is read off the ledger that did the refusing rather than off a counter kept beside it, so
the numbers a screen shows are the numbers the caps were enforced against. It is **two** ledgers
because there are two caps in the chain: `session` is this visitor's own budget (`PM_DEMO_SESSION_*`)
and `demo` is the whole demo's (`PM_DEMO_TOTAL_USD` / `PM_DEMO_DAILY_USD`). Both are shown, because
a visitor whose own budget is intact is still owed the truth when the demo as a whole is nearly
out — the refusal they are about to meet comes from `demo`, not from `session`.

**A visitor cannot reach the owner.** A session's world is built by `build_sample` with every store
`:memory:` and `path` left `None`, so "discarded when the session ends" is true by construction
rather than by cleanup. There is no route from a session to the owner's data directory, token,
audit log or agreements, and `POST /v2/demo/session/{id}/email` is the only write the visitor has.

**The world is the caregiver week, not the scheduling sample.** `build_demo_world` passes
`care_week=True` — see § "Sample mode" for what that swaps — so the `inbox` a visitor is handed is
`sim.caregiver_messages`'s four messages: the pharmacy refill, the moved physio appointment, the
transport booking, and the lookalike "benefits update" email that
`redteam/cases/09-lookalike-benefits-update.json` delivers. The care circle and the four protected
facts are seeded by `_seed_care`, which is unconditional in `build_sample`, so the care guard a
visitor reads a refusal from is refusing about THIS session's own stored facts and about nothing
else. Without `care_week=True` the page's own words were false: the screens promised a simulated
caregiver inbox and the world held one meeting with one friend in a café.

**The run answers about the message the visitor edited.** `DemoSession.run_email` selects the change
whose `message_id` is the one the request named, not the first change the watcher's tick happened to
list. The two were the same thing while the demo ran the scheduling sample — one watched message, so
a tick could raise at most one change — and the caregiver week's four messages make them different:
a tick classifies every one of them in a single pass, and a visitor who edited the lookalike email
was answered with the pharmacy refill's change. A run whose refusal is about an email nobody sent is
not a run a visitor can learn anything from. The selection is a search across every change for that
message rather than only the ones the tick raised, because the second run on the same message raises
nothing (`routine_seen` holds it) and that message's change is still the honest answer.

**The pinned event is the one the email names.** `ChangeEngine._target_event` used to fall back to
the day's EARLIEST owner-organized meeting whenever the sender was not on any candidate's attendee
list — correct for the scheduling sample's one addressed meeting, wrong for a caregiver week with
several of the owner's own appointments and a lookalike sender that (by design) matches none of
them. It now also tries the request's own words against each candidate's summary before falling
back to earliest: a lookalike "Benefits Update" sender writing about "the benefits assessment"
pins `evt-benefits-assessment` (added to `sim.caregiver_events` for exactly this), not the 09:30
transport pickup a visitor never asked about. `redteam/cases/09-lookalike-benefits-update.json`'s
`target_event` field and the demo's own built-in attack both exercise this.

**The compromised run — `POST /v2/demo/session/{id}/email/compromised`.** The product's claim is
"assume the model is fully compromised; the engine still refuses", and a real model that ignores
the injection (which real Nemotron does, on this exact email — see "2026-09-22 — public demo,
real model" in [`docs/LIVE_RUNS.md`](LIVE_RUNS.md#2026-09-22--public-demo-real-model)) never
shows a visitor that claim being tested. This route runs the
SAME edited email through the SAME session's `ChangeEngine`, `PrivacyGate` and `BoundaryStore` — the
same care circle, the same protected facts, the same calendar — with `wiring.engine.llm` swapped
for one call to `redteam.CompromisedModel`: the red-team corpus's own fully-compliant script, over a
`TokenFactory` on an `httpx.MockTransport`. For the built-in attack specifically (`msg-benefits-
lookalike`, with case 09's own injection text present in the body) it runs case 09's OWN per-phase
script — the same file the corpus scores the case against — rather than a script derived from the
text, because the caregiver week's attack asks for somebody ELSE's ("her") facts, a shape
`redteam.parse_intents` was never tuned to recognise (see `demo_session._compromised_case`). Any
other edited email gets a script derived from its own words, the same way the playground's `attack`
route does.

Zero cost by construction rather than by a counter this route remembers to skip: `CompromisedModel`'s
`TokenFactory` carries no `spend=` ledger, so it never reserves against the session's or the demo's
`SpendLedger`, and it never calls `SessionBudget.take_call()` — `calls_left` and `spend` are
identical before and after. The ROUTE is still rate-limited (`SessionBudget.take_rate`, the same
per-client bucket a real call spends from), so a visitor cannot use the free run to bypass the one
universal throttle. `attempted` names what the compliant model tried — the address it addressed the
reply to, the time it offered, that it put a "requested private detail" in the reply — and every
protected VALUE in it is replaced by its `[Label]` before the response leaves, the same "never a
value" rule `boundary._refuse` holds every refusal to.

`model` makes which model answered explicit and machine-readable rather than something a caller has
to infer from `compromised: true` alone: `{kind: "scripted", label: "Scripted compromised model —
not Nemotron", calls: 0, cost_usd: 0}` (`demo_session.COMPROMISED_MODEL_LABEL`). `calls` and
`cost_usd` are pinned at zero rather than read off any ledger, for the same reason the section above
gives for the route being free by construction — there is no ledger this run reserves against. The
real run's `DemoResult` carries the analogous `model: {kind: "real"}`, so a caller reads `model.kind`
rather than the absence of `compromised` to tell the two apart. The web UI renders `model.label` as
the compromised column's caption instead of holding its own copy of the sentence.

**It refuses to start beside a real credential.** `build_demo` calls
`demo_session.refuse_if_providers_configured` before anything is served, and `build_demo_world`
checks again per session. A `TAVILY_API_KEY`, a Google client secret or a Google account token on
disk is enough to stop it, and the refusal names what it found. A demo that starts and then 500s in
front of a judge has spent the only thing it had.

**404 outside demo mode**, not 403 — the same reasoning as the playground: off the demo there is
nothing here to be forbidden from, and a live build answering "start me a demo session" would be a
live build inviting strangers in through a door nobody audited.
