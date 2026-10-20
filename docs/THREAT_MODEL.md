# Threat model

This document says what PrivateMesh defends, against whom, and — in the last section, which is the
one to read first if you are evaluating a claim — **what it does not defend at all**.

## The one invariant

> **Nothing untrusted — the inbound email, the model's output, or code the model wrote — can widen
> what the assistant may do or reveal.**

Everything below is either an asset that invariant protects, a boundary an adversary would have to
cross to break it, or a guard stationed at one of those crossings.

## Assets

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| The **private commitment** — title, location, reason, attendees | `BoundaryStore`, `boundary.py` | It is the thing the owner most needs kept out of an outbound payload. A leaked reason is not recoverable: it is in someone else's mailbox. |
| **Memory** the owner approved | `memory.py` (SQLite + FTS5) | Recall is consent-gated; an unapproved memory must not reach a prompt. |
| The **owner's credentials** (Nebius key, Tavily key, Google OAuth refresh token) | `.env`, `secrets/` | Exfiltration is a spend and account-takeover risk. |
| The owner's **calendar and mailbox** | Gmail / Google Calendar via `gmail.py`, `gcal.py` | The blast radius of a wrong write is real, not synthetic. |
| The **audit record** | `audit.py` | It is the evidence a decision happened. If it can be edited, no other claim here is checkable. |
| The owner's **spend** | `spend.py` | A hostile input that induces a loop is a denial-of-wallet. |

## Trust boundaries

Each row is a place where untrusted data becomes trusted, or trusted data leaves. Each is crossed in
exactly one place in the code, on purpose: a boundary with two crossings is a boundary with one
unguarded crossing.

| # | Boundary | Crossed at | What is on the untrusted side |
| --- | --- | --- | --- |
| B1 | email body → engine | `agent.py`, `changes.py` | Arbitrary text, including instructions |
| B2 | engine → model provider | `llm.py::TokenFactory.chat` | Anything in the payload leaves the machine |
| B3 | model output → engine | `planner.py`, `fit.py`, `decisions.py` | A response that may have fully complied with an injection |
| B4 | engine → search provider | `boundary.py::GuardedSearch`, `tavily_evidence.py` | The query itself can leak |
| B5 | engine → calendar/mailbox | `execute.py`, `ports.py` | A write is irreversible once it lands |
| B6 | generated code → anything | `skillrt/runner.py` | Code written by the model, with the model's own intentions |
| B7 | engine → audit log | `audit.py` | Tampering after the fact |

## Adversaries

We model three, in descending order of how much they control. The third is the one most systems
forget to model at all.

### A1 — The email sender (controls B1)

**Capability.** Writes the entire body of an inbound calendar invitation. Can address it to the
owner, name any attendees, and embed instructions that read as though they came from the owner.

**Goal.** Get the assistant to move an unrelated meeting, disclose the private commitment, add a new
recipient, or change the buffer protecting the commitment.

**Modeled by.** `redteam/cases/01…08`, delivered through the real engine over real ports.

### A2 — The model (controls B3)

**Capability.** Returns anything at all — including a fully compliant response to A1's injection.
This is the adversary the design takes most seriously, because the usual mitigation ("the model
would not do that") is not a mechanism.

**Goal.** Have its output accepted as a plan or a payload.

**Modeled by.** The deterministic red-team run, which **assumes this adversary has already won**:
the scripted responses *are* the injection's request. The run then measures the only thing left —
whether the engine refuses a request it completely received. This is why the deterministic run is
the one that proves the guards, and the live run is not.

### A3 — The code the model wrote (controls B6)

**Capability.** A "learned skill" is generated code. It runs with whatever the interpreter gives it
unless something is taken away.

**Goal.** Reach the network, read a credential from the environment, escape its scratch directory,
read the tests grading it, or propose an effect its manifest never declared.

**Modeled by.** `tests/test_skillrt_escapes.py`, whose every case is paired with
`test_the_escape_is_real_when_unguarded` — an armed mutant proving the escape genuinely works when
the guard is removed, so a green result means the guard did something rather than that the attack was
feeble.

### Not modeled

A malicious *provider* (Nebius, Google, Tavily), a malicious *dependency*, a compromised *host*, and
a malicious *owner* are all out of scope. The first two are supply-chain problems, the third is a
problem no in-process guard can solve, and the fourth is the trust anchor — an owner who wants to
disclose their own commitment can simply type it.

## Guards

Each guard names the file that enforces it and the tests that would fail if it stopped working. Where
a test is an *armed mutant* — a test that fails if the guard is removed rather than merely passing
while it is present — it is marked **mutant**, because those are the tests that carry evidence.

### G1 — The protected commitment reaches the model as a busy window only

The only thing that ever leaves `BoundaryStore` is `planning_fact()`:
`{start, end, buffer_minutes, label: "protected"}` — four keys, and `_fact` has no fifth to give.
`label` is a fixed constant, never the real title. As the module docstring puts it, title, location,
reason and attendees are *"not 'hidden by the UI' — they are not in the object the planner is handed."*
`changes.py::_fact` takes the fact from the store rather than re-deriving it, so exactly one place
decides what a planning fact contains.

(The web-facing `ProtectedBlock` adds `id` as a fifth key — `tests/test_e2e_v2.py` pins
`set(block) == {"id", "start", "end", "buffer_minutes", "label"}`. The id is an opaque handle; it
carries no content.)

*File:* `src/privatemesh/boundary.py`
*Tests:* `tests/test_boundary.py::test_planning_fact_is_only_the_four_allowed_keys`,
`test_only_the_current_version_is_a_planning_fact`,
`tests/test_privacy_egress.py::test_no_protected_field_reaches_any_provider_in_a_full_run`,
`::test_the_calendar_write_carries_no_protected_field`,
`::test_the_outbound_email_carries_no_protected_field_either`

### G2 — Secrets never leave in a request body

`egress_sanitize` runs inside `TokenFactory.chat` as the last statement before the body is built, and
raises rather than returning a scrubbed copy it hopes the caller uses. Placing it in the one method
every call path already shares is the mechanism: a new call path cannot forget it.

*Files:* `src/privatemesh/llm.py`, `src/privatemesh/redact.py`
*Tests:* `tests/test_egress_shapes.py`, `tests/test_fix_egress.py`, `tests/test_llm.py`

### G3 — The private boundary is enforced on the outbound bytes, not on intent

`PrivacyGate.guard_model_payload` runs on the *sanitized* payload — the bytes that would actually be
posted — before the spend reservation and before the body exists, so a refusal costs nothing and
sends nothing. It stands in front of every model call (intent, extraction, research planning, the
answer), every Tavily query, every email subject and body, and every calendar field.

It refuses in three distinct ways, because they are three distinct mistakes:

- **`ProtectedLeak`** — a protected value appears in the payload. Whitespace and case cannot hide it.
- **`ParaphraseLeak`** — the payload does not contain the words, but shares enough of the protected
  field's *content* words to be the same disclosure reworded. A marker test cannot catch this.
- **`SchemaLeak`** — an outbound field or fragment no allow-list permits. The reply body is composed
  from declared fragments, so an undeclared one never renders.

*File:* `src/privatemesh/boundary.py::PrivacyGate`
*Tests:* `tests/test_boundary.py::test_gate_refuses_each_protected_field_verbatim`,
`::test_reworded_reason_in_the_email_body_is_refused`,
`::test_gate_refuses_a_protected_field_smuggled_into_the_subject`,
`::test_gate_refuses_a_leak_nested_in_a_list_of_parts`,
`::test_the_gate_reports_a_refusal_to_the_audit_log`

A guard that only refuses verbatim matches is trivially defeated by a paraphrase, so the refusal is
tested against a reworded leak specifically. **The honesty check is the mutant:**
`tests/test_privacy_egress.py::test_the_harness_can_see_that_leak_when_the_gate_is_blinded` proves the
leak detector is actually watching. A green run is worth nothing if the recorder was empty.

### G4 — Only declared fragments may compose an outbound email

`compose_email` builds the body from declared `Fragment`s with checked sources. Free-form prose is
refused; a fragment whose source is outside the allow-list never composes. **And the fragments
themselves have to be the ENGINE's**: `ChangeEngine._prepare` renders a catalogue of every sentence
this reply may contain (each carrying the meeting it derives from) and the draft's answer is a
SELECTION from it, so a sentence the engine never rendered has no catalogue entry and therefore no
bytes. That is the round-5 change: the boundary is composition, not a scan of the model's prose.
The scan is still there underneath, as the second net.

*File:* `src/privatemesh/boundary.py::compose_email`, `::reply_catalogue`,
`::eligible_reply_fragments`
*Tests:* `tests/test_boundary.py::test_the_gate_refuses_free_form_body_text`,
`::test_a_fragment_from_a_protected_source_never_composes`,
`::test_the_composer_refuses_a_fragment_source_outside_the_allowlist`,
`tests/test_care_r5_structural.py::test_a_sentence_the_engine_did_not_render_is_not_eligible`
*Controls:* `R5-1`, `R5-2` in `redteam/mutcheck_findings.py`

### G5 — A reply can only reach the sender of the change

`_permitted_recipients` derives the audience from the incoming message, and the check runs twice — at
compose time (where the stack still says which step built the message) and again at the port (where
it cannot be skipped).

The pin is a check on a **mutable provider record**, not on an immutable message identity: it
compares the provider's message id, thread id and sender against the pinned sender read at
detection. A provider that lets an attacker rewrite the same message record while preserving its
id can still satisfy it. That is a property of the provider, not of this code, and it is why the
audience check runs twice rather than resting on the pin alone. See "Not covered" 13 for the
normalisation limit.

*Files:* `src/privatemesh/boundary.py::guard_recipients`, `src/privatemesh/execute.py`
*Tests:* `tests/test_inject_finding1_recipient_pin.py` — the file is named rather than counted, for
the reason the mutation-control paragraph below gives: it claimed "15 tests" while the file held
sixteen, and a number beside a filename is a claim that rots on the next commit. Run
`pytest --collect-only -q tests/test_inject_finding1_recipient_pin.py` for its size on the tree you
have — `tests/test_doc_consistency.py` fails if this paragraph ever states one again. Also
`tests/test_boundary.py::test_gate_refuses_an_email_addressed_to_a_protected_attendee`

### G6 — No skill can run except through the broker

Every registered skill is wrapped by `guarded`; calling the raw executor raises `PermissionError`.
The broker writes its audit row **before** execution, so an effect that lands without a receipt is an
`UnevidencedExecution`.

*Files:* `src/privatemesh/broker.py`, `src/privatemesh/skills.py`
*Tests:* `tests/test_bypass.py::test_every_registered_skill_is_broker_gated` **mutant** — designed to
fail the moment someone registers a bare executor — and
`::test_calling_a_skill_executor_directly_is_refused_and_unreceipted`

### G7 — Every side effect is a policy decision: ALLOW / NEEDS_APPROVAL / DENY

The policy table is consulted per step. `interactive=False` models an unattended run and turns every
`NEEDS_APPROVAL` into a hard `DENY`, so nothing waits forever on an owner who is not there. Autonomy
grants can only ever tighten.

*Files:* `src/privatemesh/policy.py`, `src/privatemesh/broker.py`
*Tests:* `tests/test_policy.py`, `tests/test_broker.py`

### G8 — An approval is bound to the exact arguments and is spendable once

`args_fingerprint` hashes the canonical arguments; `consume` refuses an approval whose fingerprint no
longer matches. An approval covers one action, not a class of actions. The executor additionally
refuses an approval that refers to an **older version** of the bundle, and refuses a boundary the
owner edited after reviewing.

*File:* `src/privatemesh/approval.py`
*Tests:* `tests/test_fix_args_domain.py`, `tests/test_fix_approval_persistence.py`,
`tests/test_e2e_v2.py` (the stale-version and edited-boundary refusals),
`tests/test_changeset.py`

### G9 — Generated code runs isolated, and is graded by tests it never saw

`LocalIsolatedRunner` spawns a separate interpreter with `-I -S -B`, a fresh empty cwd, an
environment inheriting **nothing**, OS resource caps, and an audit hook armed before the skill is
imported (plus a network-deny `sandbox-exec` profile on macOS where available). Escapes are stopped
**and named** from a small vocabulary in `violations.KINDS`, so the owner's report can say "tried to
open a network connection".

The grading property is structural, not procedural: a generator that writes both a skill and its
tests has proved nothing, so the harness grades against held-out cases, and the held-out directory
is on `sys.path` only while the test module is imported — **revoked before the graded function is
called**. A run with no held-out cases is not a pass.

*Files:* `src/privatemesh/skillrt/`
*Tests:* `tests/test_skillrt_escapes.py` (each case paired with the **mutant**
`test_the_escape_is_real_when_unguarded`; also
`::test_a_skill_cannot_read_the_held_out_tests_that_grade_it`),
`tests/test_skillrt_harness.py::test_a_run_with_no_held_out_cases_is_not_a_pass`,
`::test_held_out_tests_inside_the_skill_directory_are_refused`,
`::test_the_harness_catches_what_the_generators_own_tests_missed`,
`::test_env_is_not_inherited_at_all`,
`tests/test_skillrt_contree.py`

### G10 — The audit log is append-only and tamper-evident

Every row carries `sha256(prev_hash + canonical_json(row))`; `verify()` names the **first** broken
row. A fresh empty log is witnessed at birth (`rows=0`, tip = `GENESIS`) so a truncated log is
detectable rather than merely short.

*File:* `src/privatemesh/audit.py`
*Tests:* `tests/test_audit.py::test_verify_catches_a_tampered_row` **mutant**,
`::test_verify_catches_a_deleted_row` **mutant**,
`::test_verify_catches_a_row_appended_without_the_chain` **mutant**,
`tests/test_audit_genesis.py::test_a_log_with_rows_whose_checkpoint_vanished_still_fails`,
`tests/test_fix_audit_concurrency.py`, `tests/test_fix_audit_checkpoint.py`

### G11 — Spend is capped before the request, not audited after it

`SpendLedger.reserve` computes worst-case USD from the prompt length and `max_tokens` and checks the
cap **before** anything is sent, so `SpendCapReached` means no request was made.

*File:* `src/privatemesh/spend.py`
*Tests:* `tests/test_spend.py`

### G12 — The model's reasoning text never propagates

`_answer_text` pops `reasoning_content` off the message before the caller holds it and keeps only its
length. The scratch work is not in the object the caller has, so it cannot be echoed into a repair
transcript, a memory, or an audit detail.

*File:* `src/privatemesh/llm.py`
*Tests:* `tests/test_llm.py`

### G13 — Recall is consent-gated and receipted before the model sees it

Recall filters on `approved = 1 AND invalid_at IS NULL` as a non-parameter clause, and the ids
recalled are receipted **before** the model is called — so "the model saw X" is a record, not a
reconstruction. Forgetting sets `invalid_at`; it never deletes, so history stays inspectable.

*Files:* `src/privatemesh/memory.py`, `src/privatemesh/broker.py::record_model_saw`
*Tests:* `tests/test_memory.py`, `tests/test_fix_receipt_order.py`, `tests/test_fix_model_saw.py`

### G14 — Ambiguous is not success, and there is no automatic retry

A calendar write that times out ambiguously is reconciled by idempotency key; an unknown delivery
never becomes a successful send and never becomes an automatic retry. A read-back confirmation is
required before an effect is recorded as landed.

*File:* `src/privatemesh/execute.py`
*Tests:* `tests/test_execute.py`, `tests/test_e2e_v2.py`

### G15 — Acting as the owner requires the owner's credential

Reaching the port is not being the owner. The server mints a 256-bit token at startup
(`secrets.token_urlsafe(32)`) into `~/.privatemesh/owner.token` — file 0600, parent directory 0700,
location overridable with `PM_OWNER_TOKEN_FILE` — and a caller proves it is the owner by presenting
that token as `Authorization: Bearer <token>`. Comparison is `hmac.compare_digest` over encoded
bytes, and the header parse is exact (one space, no trimming, no second spelling), because a
credential with two spellings is a credential with an untested one. **Twenty routes are gated**:
`prepare`, `leave`, `edit`, `approve`, `decisions`, `set_routine`, `set_boundary`, `set_limits`,
`limits_state`, `add_preference`, `update_preference`, `delete_preference`, `learn`,
`approve_learning`, `reject_learning`, `revoke_skill`, `teach`, `model_view`, `agreements`,
`check_agreements`.

The number is worth stating precisely because it has been wrong here twice in opposite directions:
this paragraph said "sixteen" while the bullet below said "fourteen", and both were stale — the
standing limits, Teach Mode and the model view each added a gated route without moving either
number (`limits_state`, `set_limits`, `teach`, `model_view`). The list above is the count the code
has: twenty `V2Service` methods call `require_owner`, and every one of them is reachable from the
router. A checklist built on a number that drifts is worse than no number, so it is now the one the
router actually serves rather than a figure carried forward.

The last two were added with the agreements feature (2026-09-19, G16). `check_agreements` reads the
owner's calendar and writes nothing outward, so it is gated on the question that actually decides
it — who may ask this product to go and look at your meetings — rather than on whether the call is
harmless. "It only reads" is a property of today's implementation, not a promise about tomorrow's.

Two things about it are load-bearing:

- **A live server with no readable token refuses all twenty.** There is no flag that turns the
  gate off, and the token is not a value the code asserts about itself — the previous design was
  `build_live(owner_session=True)`, a boolean set at startup that made every request reaching the
  port the owner's, which is not authentication but a comment.
- **The token is stable across restarts,** because a restart that minted a new one would lock the
  browser out and the natural fix for that is the flag above.

Sample mode is deliberate and narrow: its owner is the person running the demo, on their own
machine, against synthetic data, and it has no providers to reach, which is why it is the one mode
where ownership is a mode rather than a secret.

**`demo` mode is the second, and it is narrower still** — see G19. It serves four public routes
whose whole point is to be reachable by a stranger, and the answer to "then who may do what" there
is not the gate but a budget. Every owner route on a demo host still refuses, including the reads,
because a demo host is built with no owner token and `is_owner` is false for every caller.

Two boundaries hold that list up, and they are separate on purpose. The gate is a **route class**
set as the router's default, so a route registered through the router is gated without anyone
remembering to say so; and a **per-request middleware** resolves the route that is actually about to
answer and refuses unless that route proves gated or proves it is one of the vetted public routes
(`PUBLIC_V2_ROUTES`: the playground's, and the demo's four — see G19) —
both by identity, so neither a copied mark nor a route merely spelled like the exempt one is
accepted. It judges every scope that carries a path, not only HTTP, and it has to be the innermost
middleware: it mirrors the router's own matching, so its answer agrees with the dispatch only while
nothing runs between the two. Two **uncovered** residuals of that design — code inside the process
rebinding a route's own dispatch after the gate has approved it, and code inside the process
inserting a middleware into the stack after the gate's position has been checked — are stated, with
their reasoning and what would close them, as item 22 under "Not covered". Neither is guarded
against, deliberately.

**The care review asked whether that openness extends to the values.** It does not, and the decision
is written here rather than left to be inferred from the tests. Opening sample mode means *nobody is
authenticated*; it must not also mean *the facts are readable*. Those are two different relaxations
and only the first is defensible:

- **Kept open:** `require_owner` does not demand a token in sample mode. The demo has no credential
  to present, and a screen that showed the care circle and the protected-fact list is the point of
  the demo.
- **Not kept open:** every route that could answer a protected VALUE is checked in BOTH modes. The
  fact store has no method that returns a value at all (`ProtectedFactStore.protected_values()` is
  the one exception, and it exists to feed the masker and the gate), `as_dict()` carries the label
  and the kind and never the value, and `model_view` runs `_gate_prompts` in both modes — with the
  engine's gate, which knows the care facts, rather than one built from the boundary alone.
- **Why the asymmetry is safe rather than lucky:** the redaction is on the *data*, not on the
  *request*, so it does not care which mode it is running in. A route that is safe only because a
  caller was authenticated would be one `if` away from serving a value; a route that cannot return
  the value because no code path returns it is not.

The honest statement of the residual: sample mode is unauthenticated **and** its data is synthetic,
so an attacker who reaches a sample-mode server learns the fixture values and nothing about the
owner. The guard is what makes the second clause true if the first stops being — and
`tests/test_care_review_findings.py::test_finding_14_no_route_returns_a_value_in_either_mode`
asserts exactly that, in both modes, with the owner presented and without.

*Files:* `src/privatemesh/owner_token.py`, `src/privatemesh/api_v2.py`, `src/privatemesh/wiring.py`,
`src/privatemesh/care.py::ProtectedFactStore`
*Tests:* `tests/test_fix_owner_token.py`, `tests/test_fix_route_gate.py`,
`tests/test_care_review_findings.py::test_finding_14_no_route_returns_a_value_in_either_mode`

### G17 — The care circle is the audience, and the digest is composed from records

A care action — a digest, a reply to a member, a scheduling note to a member — may reach people on
the owner's circle, for the kinds the owner listed each of them for, and nothing else. The digest
additionally may go out once a day.

Three properties, each structural rather than procedural:

- **The audience comes from the circle, not from the caller.** `Executor.build_digest` reads
  recipients from `CareCircleStore.members()` filtered by `may_receive(..., "digest")`. No route
  accepts a recipient list; the only input to `POST /v2/care/digest` is the day.
- **Membership and grant are answered from ONE reading.** `CircleSnapshot` is taken once and both
  questions are answered from it, at composition and again at the port. Two reads of one immutable
  snapshot is still one read; the second, fresh snapshot immediately before the port is what catches
  a member removed in between.
- **The once-a-day cap is taken by an INSERT, not by a count.** `care_digests` carries a UNIQUE
  index on the day, so two concurrent runs produce one row and one `DigestAlreadySent` — a
  read-then-write would let both proceed. The row is `confirm`ed only after the port returned and
  `release`d on any exception, so the ledger records what was SENT and not what was attempted. A
  missing ledger and a missing day both fail **closed**: "the limit cannot be checked" is a refusal.
- **A digest line is rendered from a record, never authored.** Every `DigestLine` carries the
  `record_id` it came from, and `compose_digest` refuses a fragment source outside the allowlist, so
  a model has no path to write a sentence into a digest: it cannot supply a record id.

*Files:* `src/privatemesh/care.py`, `src/privatemesh/boundary.py::PrivacyGate.guard_care_recipients`,
`src/privatemesh/execute.py::Executor.build_digest`, `::Executor.send_digest`
*Tests:* `tests/test_care.py`, `tests/test_care_review_findings.py` (findings 9–13)

### G16 — Noticing drift is not acting on it, and an unreadable meeting is never "unchanged"

The agreements feature re-reads the events the owner agreed to and compares them field by field.
Two properties are load-bearing, and both are structural rather than procedural:

- **`AgreementChecker` holds no mail client, no model and no broker, and may call only reads on the
  calendar.** "Drift never causes an action" is therefore a property of the object graph, not a rule
  somebody has to keep. It matters because of who can cause drift: **A1, the email sender, can edit
  the invite after the owner agreed to it** — every attendee with write access to an event can,
  which is a wider set than the correspondent. A feature that reacted to drift would hand that
  adversary a way to make this product write to the calendar or send mail, using an edit the owner
  never approved. The comparison's entire output is information for the owner.
- **A read that fails is `unchecked`, never `holding`.** A meeting the product cannot read is not a
  meeting that still matches. `observed` is cleared so no stale reading is served as a current one,
  and `note` distinguishes "not checked yet" from "could not be read" — only one of those resolves
  itself. Reporting `holding` over a failed read would be the product asserting a match it never
  checked, which is the same class of error as reporting a calendar effect landed when the read-back
  never confirmed it (G14).

The event's title and description are **untrusted** and never reach a model here; this module has no
model client at all. An agreement is recorded only where the invite demonstrably moved — the write
was accepted **and** the read-back showed it — so an effect that failed, was refused, or was
accepted-but-not-applied leaves nothing behind. And the comparison is deliberately conservative in
one direction: `location` is compared exactly, so two spellings of one venue read as drift. That
under-reports nothing and over-reports a real difference; inventing a notion of "the same place"
would over-report nothing and under-report an edit the owner cares about.

*Files:* `src/privatemesh/agreements.py`, `src/privatemesh/execute.py`, `src/privatemesh/api_v2.py`
*Tests:* `tests/test_agreements.py` — mutation controls `AGREE-1`…`AGREE-23` in
`redteam/mutcheck_findings.py`, one per guard below, re-runnable with
`python redteam/mutcheck_findings.py`. The claim used to say "8 mutation controls" while the
repository contained eight ordinary tests and no mutations to run; a reviewer could neither rerun
nor identify them, which made it a claim about intent rather than evidence about bytes. The count
of controls that are actually caught is printed by that command and is deliberately not restated
here — the round-2 review found the runner counting a collection error, a misspelled node id and a
fixture failure as coverage, and the honest number depends on the run, not on this paragraph.
`verdict()` in the same file now requires the named test to have RUN and failed on an assertion
(`tests/test_mutation_runner.py` holds that rule against captured pytest output), and it sorts a
non-assertion failure into a third tier that is reported and NOT counted: a control whose test goes
red only because the mutant makes it crash tells you the guard is reached and nothing about what
the test claims, and the fix for that is in the test. The command prints both numbers for that
reason.

**Round 8 re-ran the whole set (`--all`, 175 controls as it stood then; 191 now) and three controls
had to be dealt with rather than counted.** Two are repaired: `CARE-7`'s guard text had gone stale
when round 7 changed `artifact_id`'s payload (it now matches, and the control is RED), and `CARE-16`
went uncovered because round 8's rewrite of its named test left the allowlist rule asserted only
against the constructor that mints digest lines and no longer against
`boundary._check_fragment_sources` — the test now asks the egress path's own door, with a real
subject template so the subject check cannot refuse first and let the assertion pass for the wrong
reason.

**The third was `R4-1`/`R4-2` — the calendar foreign-field guard, at the gate and at the port — and
it is now closed.** Round 8 left both uncovered and named them here rather than dropping them
silently: both named tests passed with the guard removed, because `_venue_not_rendered` (added in
round 7) refused the same fixture earlier in `update_event`. The guard was present and load-bearing;
what was missing was a test that reached it. A baseline run at `73b982e` confirmed both were already
in that state before round 8 touched anything, so it was a pre-existing gap in the evidence and not
a regression — and it stayed open until this round, which is what the paragraph you are reading was
waiting for.

What closes it is
`tests/test_care_review_r4.py::test_finding_01_the_foreign_venue_guard_is_reached_with_the_render_check_satisfied`,
which both controls now name. It builds the state that gets PAST the render check: a change that
chose a catalogue record whose render is the other meeting's place — what the engine produces when
research offers that meeting's own page as a venue candidate and the model picks it. Every
engine-side question about where the string came from then answers correctly, and the only question
left is the one this guard asks and the render check does not: is this string, whatever record it
came from, somebody else's meeting? With `foreign=()` at either site the write reaches the provider
and the address lands on the pinned invite with `send_updates="all"` behind it; with the guard intact
it is refused. Both controls are RED under `--all`, and the test is green on the real code.

The general lesson is recorded here because it is the one that cost the evidence: **a control whose
named test is green is not a control**, and the reason these two were green was a SECOND, different
guard standing in front of the first. That is also why the twins a rewrite leaves behind —
`CARE-10`/`CARE-10b` on the protected-fact store and `DEMO-9`/`DEMO-9b` on the two demo email routes
— each carry their own control rather than sharing one.

| | guard |
| --- | --- |
| `AGREE-1` | a stale check cannot overwrite a newer agreement (the compare-and-swap) |
| `AGREE-2` | the agreement records the reviewed guest list, not the read-back |
| `AGREE-3` | the agreement records the reviewed title, not the read-back |
| `AGREE-4` | a record this build cannot read is served, not omitted |
| `AGREE-5` | the check ROUTE reaches no provider for a write |
| `AGREE-6` | the read-back confirms the window as instants, not as strings |
| `AGREE-7` | guests compare caselessly, not merely lowercased |
| `AGREE-8` | the LIVE composition roots agreements in the data directory |
| `AGREE-9` | the compare-and-swap writes conditionally on the row it read (one statement) |
| `AGREE-10` | an older execution cannot replace a newer agreement (the execution sequence) |
| `AGREE-11` | a bundle that never named its guests records none of them |
| `AGREE-12` | a notifying bundle that names nobody is refused at approval |
| `AGREE-13` | a row's key is bound to the id inside its payload |
| `AGREE-14` | a store failure after the effect landed does not abort the run |
| `AGREE-15` | a status this build does not have is unreadable, not downgraded |
| `AGREE-16` | a field this build does not write is unreadable, not read as a subset |
| `AGREE-17` | a later schema version is unreadable, not read as this build's |
| `AGREE-18` | the swap is retried until it wins or the stored row is newer, not twice |
| `AGREE-19` | a swap that runs out of time raises instead of reporting a dropped write |
| `AGREE-20` | a run that cannot be numbered does not touch a provider |
| `AGREE-21` | the fallback row keeps the guest list the owner reviewed |
| `AGREE-22` | a fallback row for a bundle that named nobody is unreviewed |
| `AGREE-23` | a stored `unreviewed` that is not a boolean is unreadable |

### G18 — Every outbound field, at every port, is an engine render or the action is refused

Round 6's rule, stated once and enforced at both ports:

> Every outbound field, at every port, is either an engine-rendered fragment carrying the event id it
> derives from, or the action is refused. The day/window a guard reasons about comes from trusted
> state, never from caller input.

The pressure that produced it is that a port is the last place a bundle can be judged, and a port
that judges the ARGUMENTS it was handed is judging a value it took on trust. Four doors were open
before this round and each is now closed at the line that reads the field:

| port | field | the rule at the port | refusal |
| --- | --- | --- | --- |
| `mail.send_reply` | `body` | the message is REBUILT from the change's own stored decomposition (`_outbound_from_bundle`) and `body == rendered()` is decided against that, not against `args` | `SchemaLeak` |
| `mail.send_reply` | `subject` | the foreign-event question is asked of `subject` and `body` together — a header is read by a recipient | `ForeignEventLeak` |
| `mail.send_reply` | `to`, `in_reply_to` | the audience is re-derived from the provider against the approved change | `RecipientRefused` |
| `mail.send_reply` | the day/window | the day comes from the PINNED EVENT placed in the owner's zone; the offset from that zone (or, for an offset-configured build, from the provider-issued stamp of that same event). `care_day` and `change.proposal["start"]` are not read | `CalendarDayUnknown` |
| `calendar.update_event` | `location` | the value must be `render_venue(venue_record(change.venue_catalogue, change.venue_id))` — the model selects an index, it does not author a place | `CalendarRefused` |
| `calendar.update_event` | the day/window | the same derivation as the reply's; the write's own `start` must additionally be an instant this build can place | `CalendarDayUnknown` |
| `compose_digest` | each line, and its fragment's `event_id` | a line is a `DigestLine` (never a string), its source must match the list it was passed in, its `record_id` must exist when the caller offers a record set, and the id travels ONTO the fragment | `DigestBadLine` |
| `CareRefused` | the digest's audience | the circle, read once into a snapshot, checked at composition and again at the port against the same reading | `CareAudienceRefused` |

**The rejection that matters is "the two moments are one function".** `PrivacyGate.guard_email` is
called by `compose_email` at `validate` and by `send_reply` at `send`; the port runs no check of its
own. A port with its own thinner copy of the rule is how the reviewed build ran `check_text` over a
body nobody had asked a fragment about.

*Files:* `src/privatemesh/execute.py::send_reply`, `::update_event`, `::_outbound_from_bundle`,
`::_venue_not_rendered`, `::_local_day`, `::_day_window`; `src/privatemesh/boundary.py::guard_email`,
`::foreign_fragments_of`; `src/privatemesh/changes.py::venue_catalogue`; `src/privatemesh/care.py::compose_digest`
*Tests:* `tests/test_care_review_r6.py` (13 tests, one per door plus a control for each)
*Controls:* `R6-1a`–`R6-5` in `redteam/mutcheck_findings.py` — 9/9 RED
*Probes:* `tools/r5_probes.py --doors` — one attack and one control per field, 9/9 refused, no control refused

**What this does NOT cover** is stated in "Not covered" 30 rather than left implied: the foreign
set is narrowed by the correspondent's own message, by content. The other two gaps this block
named — the venue rule binding only where a selection existed, and `compose_digest` trusting a
caller's record set — were closed in round 7 and are recorded as closed in items 28 and 29.

### Round 7 — every field, not only the rendered ones

> Every outbound field, at every port, is engine-rendered with provenance, DERIVED FROM THE APPROVED
> PLAN, or computed at send time for a reason that is stated. Nothing else reaches a provider.

Round 6 closed the body and left the fields around it. Four were outside the rule:

| port | field | the rule at the port | refusal |
| --- | --- | --- | --- |
| `mail.send_reply` | `subject` | built from the change's own `subject_parts` (`SUBJECT_TEMPLATES` prefix + declared pieces) and compared with `subject_rendered()` at the gate — the same equality the body has had | `SchemaLeak` |
| `care.digest_email` | `subject` | the render of the one fixed prefix (`DIGEST_SUBJECT_TEMPLATE`) and the plan's own day; there is no `subject` parameter to pass | `SchemaLeak` |
| `mail.send_reply` | `In-Reply-To`, `References` | built from the PROVIDER'S copy of the pinned message, and the approved `rfc822_message_id` must be that same record — a caller-supplied value is never read | `ApprovedArgumentsRefused` |
| `mail.send_reply` | `X-PrivateMesh-Key` | the approved bundle's `idempotency_key`; a caller-chosen one decides how many copies the recipient gets | `ApprovedArgumentsRefused` |
| `gmail.GmailClient.send_reply` | the HEADER SET | closed: every name on the wire is read off the one builder (`reply_header_names()`), and a name outside that set is refused with the name in the message | `GmailError` |
| `calendar.update_event` | `event_id`, `if_match_etag`, `start`, `end`, `location`, `send_updates` | each compared with the value on the APPROVED bundle before anything is written; no approved bundle is a refusal | `ApprovedArgumentsRefused`, `CalendarRefused` |
| `calendar.update_event` | `location` (provenance) | a change with no venue catalogue, or no selected record, is now REFUSED rather than reported clean — "there is no record to compare against" is not "the value was checked" | `CalendarRefused` |
| `compose_digest` | each line's text | the line must be the RENDER of the record it names; `record_id` was required and then dropped, so a citation proved nothing | `DigestBadLine` |
| `compose_digest` | the artifact hash | covers `event_id` as well as the words, so which record a sentence claims to have come from is part of what an approval is keyed on | — |

**The three statuses, and where each field sits.** Every field on the wire is one of:
*engine-rendered with provenance* (`subject`, `body`, `location`, each digest line), *derived from
the approved plan* (`to` from the correspondent, `In-Reply-To`/`References` from the pinned message,
the idempotency key, and the six calendar engine fields), or *computed at send time* — and the only
fields in that third group are the ones the engine cannot know before the moment of sending:
`Message-ID` (the provider's, minted by the transport), `Date` (the provider's), and the day/window
the foreign check reasons about (read from the pinned event under the owner's zone at the moment of
the call, because a calendar other people are editing has to be read then). None of the three is a
place a caller can write.

> **CORRECTION (round 8).** The paragraph above, as written in round 7, was wrong in two ways, and
> the eighth review found both. **(a)** It named a *complete* partition into three statuses when two
> fields sat outside all three. `calendar.update_event`'s `start.timeZone`/`end.timeZone` was
> written by the adapter from the event it read at the moment of the call — not engine-rendered with
> provenance, not derived from the approved plan, and not one of the three send-time fields named
> here, since it is content the plan could have carried. And `to` was listed as *derived from the
> approved plan* while only the bare ADDRESS was bound: the display name the adapter serialises was
> unbound, so `Finance <alex@example.com>` replaced an approved `alex@example.com` on the wire.
> A field half-covered was reported as covered, which is the failure this section exists to prevent.
> **(b)** "Derived from the approved plan" described a binding that was made ONCE and never
> re-asked, so a revision landing between the port's binding read and its provider call left v1's
> bytes leaving under an approval the owner had replaced.
>
> Both are fixed in round 8. `timezone` and the recipient's full representation now sit in *derived
> from the approved plan*, and the two send-time fields the engine computes are the three named
> above and nothing more. What is also now stated plainly is that the partition is **not the whole
> claim**: two further questions are asked at the ports that are not questions about a field at all.
> *Who built this plan* — `Executor.send_digest` will not send a `DigestPlan` this engine did not
> compose from its own reads, whatever the plan's pieces say (`DigestPlanForged`). And *is this
> still the version the approval was raised over* — a port that bound its fields to one version
> refuses if the change has moved since (`ApprovedArgumentsRefused`). A field-by-field table cannot
> express either, which is why the round-7 table read as complete while both were open.

*Files:* `src/privatemesh/boundary.py::render_subject`, `::SUBJECT_TEMPLATES`, `::subject_rendered`,
`::guard_email`; `src/privatemesh/changes.py::subject_parts`; `src/privatemesh/care.py::digest_subject`,
`::digest_subject_fragments`, `::artifact_id`; `src/privatemesh/execute.py::_bind_field`,
`::_approved_arguments`, `::CALENDAR_BOUND_FIELDS`, `::EMAIL_BOUND_FIELDS`, `::ApprovedBundles`;
`src/privatemesh/gmail.py::reply_header_names`, `::assert_approved_headers`, `::APPROVED_HEADERS`;
`src/privatemesh/changeset.py::for_change`
*Tests:* `tests/test_care_review_r7.py` (25 tests: one per field, plus a control for each and one
structural test per closed set)
*Controls:* `R7-1a`–`R7-4d` in `redteam/mutcheck_findings.py` — 14/14 RED; round 6 re-run at 9/9 RED
*Probes:* `tools/r5_probes.py --doors` — 23 outbound field(s) probed at the close of round 7, every
attack refused, every control accepted. The table now carries 27 (round 8's four rows); the r7 rows
are unchanged and still pass.

**What this does NOT cover:** the foreign set is still narrowed by content (item 30), and the
day/window a guard reasons about is still read from the provider at the moment of the call rather
than pinned with the approval (item 32).

### Round 8 — the origin, the version, and the two fields the table was silent about

> A plan must be unforgeable, and a binding is a claim about a VERSION, not only about a value.
> Every outbound field is the value the owner approved, on the plan the owner approved, at the
> version they approved it at — or nothing is sent.

Round 7 closed the fields and left three things outside them. The first is not a field at all.

| # | where | the door | the rule now | refusal |
| --- | --- | --- | --- | --- |
| 1 | `care.digest_email`, `execute.send_digest` | the PLAN's origin | the plan carries an HMAC seal over its own artifact id, keyed on a per-process secret; `send_digest` re-derives it under a SECOND, per-executor key that `compose_digest` cannot mint. A plan that satisfies every content check and no composer built it is refused | `DigestPlanForged` |
| 2 | `mail.send_reply`, `calendar.update_event` | the VERSION bound | the version is carried out of the same read the fields came from, and re-read immediately before the provider call; a change that has moved since is refused | `ApprovedArgumentsRefused` |
| 3 | `mail.send_reply` | `to` (display name) | the FULL representation the adapter serialises is bound, not the bare address — `Finance <alex@example.com>` and `alex@example.com` are two different arguments | `ApprovedArgumentsRefused` |
| 4 | `calendar.update_event` | `start.timeZone`, `end.timeZone` | the zone is on the approved bundle and compared like the times it interprets; the adapter no longer supplies it from its own read. A provider serving a different zone is refused rather than overruled | `ApprovedArgumentsRefused`, `CalendarError` |

**Why finding 1 needed two keys and not one.** `compose_digest` is a public function taking a
`records` mapping, so a caller can compose a plan from a record set of its own making and satisfy
every check the composer makes — a seal that only proves "a composer built this" would have left a
one-line variant of the demonstrated door open. The composer's seal (`care._PLAN_KEY`) answers *a
composer built this*; the port's (`Executor._digest_key`) answers *THIS engine built this, from the
calendar, the change store and the circle it holds*. Neither is a flag: a caller cannot compute
either without the key, and both are compared with `hmac.compare_digest`.

**Why the zone is BOUND rather than argued safe.** The tempting argument was that the provider's own
ETag already refuses a write against an event that moved, so the zone could be read at send time.
That argument rests on provider semantics this build cannot verify from here, and the zone decides
which wall clock the approved `start` lands on — so an unverifiable premise was traded for a bound
field instead.

*Files:* `src/privatemesh/care.py::plan_seal`, `::seal_plan`, `::plan_is_sealed`, `::port_seal`,
`::seal_for_port`, `::plan_is_port_sealed`, `::DigestPlanForged`; `src/privatemesh/execute.py::`
`_approved_bundle`, `::_still_at_the_bound_version`, `::CALENDAR_BOUND_FIELDS`, `::EMAIL_BOUND_FIELDS`;
`src/privatemesh/gcal.py::update_event`; `src/privatemesh/changes.py::_prepare`;
`src/privatemesh/boundary.py::_CALENDAR_ENGINE_FIELDS`
*Tests:* `tests/test_care_review_r8.py` (17 tests: four findings, a control for each, and the exact
interleave for the version — the revision is triggered from the port's Nth read, and the test
asserts the read count so it cannot quietly stop testing the window it names)
*Controls:* `R8-1a`–`R8-4b` in `redteam/mutcheck_findings.py` — 7/7 RED; rounds 6 and 7 re-run at
9/9 and 14/14
*Probes:* `tools/r5_probes.py --doors` — 27 outbound field(s) probed, every attack refused, every
control accepted

**What this does NOT cover:** the check at the call is not atomic with the call. A revision landing
between `_still_at_the_bound_version` and the provider's own receipt is not caught by it — what
bounds that window is the provider's `If-Match` on the calendar write and the idempotency key on the
reply, neither of which is something this process can widen (item 33).

### G19 — The public demo is public on purpose, and bounded instead of gated

A judge with no credential must be able to run the product. That is the requirement the previous
submission failed, and a demo whose first screen is a login is a README with extra steps. So the
demo mode serves **four routes with no owner token**:

| Method + path | why it is public |
| --- | --- |
| `POST /v2/demo/session` | a stranger must be able to begin. The resource at risk is this process's memory, which is what the session cap and TTL bound |
| `GET /v2/demo/session/{session_id}` | that session's own state. The id is a 192-bit server-minted secret (`secrets.token_urlsafe(24)` — 24 random bytes, not the owner token's 32; pm-demo-r3 finding 7 corrected this line rather than the code, since 192 bits is already far past any brute-force budget a 30-minute-TTL, rate-limited session needs) the caller was just given, and it names a world this session created |
| `POST /v2/demo/session/{session_id}/email` | the point of the mode: rewrite the inbound email and run the real pipeline over it |
| `POST /v2/demo/session/{session_id}/email/compromised` | the same email through the red-team corpus's own fully-compliant scripted model instead of the real one. Public for the same reason as `/email` — it is the other half of the same claim, shown rather than asserted — and it is the narrowest of the four: no model call is spent, so it touches neither the spend ledger nor the session's call budget |

They are listed in `api_v2.PUBLIC_V2_ROUTES` — the same table the gate reads — and the exemption is
granted by route **identity** at the moment the gated router builds the route, not by spelling the
path (the eighth review's finding; the same mechanism the playground uses). The exposure is not
the gate's absence but the four caps underneath it, and each is enforced in code, is bounded above,
and resolves to its SMALLER value when its setting is missing or unreadable:

- **The model is real and the providers are not.** The demo's model is a real `TokenFactory` against
  Nebius Token Factory; the inbox, the calendar and web search are `SimMail`/`SimCalendar`/
  `SimSearch`. There is no path from a session to a provider adapter, and the strongest reason is
  that there is no credential in the process to reach one with.
- **It refuses to start beside a real credential.** `build_demo` calls
  `demo_session.refuse_if_providers_configured()` before anything is served, and `build_demo_world`
  checks again for every session. `TAVILY_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`, a Google
  client secret or a Google account token on disk under `PM_GOOGLE_TOKEN_DIR` is enough to stop it.
  A demo that starts and then 500s in front of a judge has spent the only thing it had.
- **A visitor cannot reach the owner.** A session's world is built by `build_sample` with every
  store `:memory:` and `path=None`, so the session's data is discarded by construction rather than
  by cleanup. Nothing in the session reads the owner's data dir, token, audit log or agreements,
  and the only write a visitor has is the body of one email in their own world.
- **Spend is capped before the request, by the existing ledger.** The session's `SpendLedger` is
  chained to the process-wide one (`ChainedLedger`), and both are reserved against before the
  request body exists — the same `SpendLedger.reserve` the live product uses, not a second counter
  beside it. A refusal rolls the session's reservation BACK, so a visitor who exhausts the shared
  budget cannot ratchet it, and an unpriced model is refused rather than counted as free.
- **A cap reached degrades, it does not fail and it does not go quiet.** The run completes against
  the scripted sample path and the answer carries `budget.degraded = {reason, detail}` naming the
  setting that bound. A visitor is never refused silently and never overspent for.
- **The rate limit is keyed on the peer address, not `X-Forwarded-For`,** unless `PM_TRUST_PROXY`
  turns that on — and when it does, the hop read is counted back from the RIGHT by
  `PM_TRUST_PROXY_HOPS` (default 1), because the leftmost hop is the one the client wrote. Reading
  the first hop, which this did before pm-demo-r3, let a visitor mint a fresh bucket per request by
  varying a forged prefix. The table is bounded and evicting, so the endpoint is not a
  memory-growth primitive.
- **One body, bounded.** `body_text` is refused above `playground.MAX_EMAIL_CHARS` (2,000
  characters) rather than truncated, so the cost of a run stays the cost the README's arithmetic
  is built on; an unbounded body would have made the per-run figure describe nothing.

**What is NOT claimed.** The demo is not a sandbox for the host: the model is real and a stranger
writes its input, so the email body reaches a real provider and a real bill. What bounds that is
`PM_DEMO_SESSION_USD` × `PM_DEMO_MAX_SESSIONS` plus the process-wide ceiling, and those are the
numbers an operator must size for the traffic they expect rather than assume. **Nothing about a
multi-process deployment — the demo REFUSES to run as one.** The session table and the rate buckets
are per-process, so N workers would serve N times the sessions and N times the rate while printing
one process's caps. `wiring.build_demo` therefore calls `demo_session.refuse_if_multi_worker()`,
which reads `PM_DEMO_WORKERS`, `WEB_CONCURRENCY`, `UVICORN_WORKERS`, `GUNICORN_WORKERS` and
`WORKERS`, and refuses on any value other than 1 — including a value it cannot parse, because an
unreadable worker count is not evidence of a single worker. What survives scaling out regardless is
the process-wide spend ledger, and it is the one that stops the money. **Nothing about the demo's
response to a hostile model:** an injection that gets through in a session reaches simulated
providers with nothing behind them, which is what makes it safe to let a stranger try.

*Files:* `src/privatemesh/demo_session.py`, `src/privatemesh/api_v2.py`, `src/privatemesh/wiring.py`
*Tests:* `tests/test_demo_mode.py` (each cap proven by the count that reached the transport, with a
mutation control that lifts the cap and must reach it), `tests/test_fix_route_gate.py`

### The races this feature is built around

Drift detection is a read of a world that other people are editing, so the failures worth naming
are the ones where two things happen at once rather than where one thing is wrong:

- **A check and an execution, on the same event.** `check` reads a record, goes out to the provider
  and writes back what it found; an execution in that window records a NEWER agreement. The check's
  answer is about a record the owner no longer holds, and writing it would restore the superseded
  window as the new baseline — permanently, because every later check compares against it. Every
  write carries the revision it read and is dropped when the row has moved on (`AGREE-1`), and the
  drop is decided by ONE conditional statement rather than by a read followed by an unconditional
  write — revisions kept in one process's memory are not a fact two server workers can share
  (`AGREE-9`).
- **An execution and a LATER execution, on the same event.** The run that pauses between its
  confirmed write and its record resumes holding the window the world has already left, and because
  it writes last it carries the higher revision — so revision ordering hands the stale run the win.
  Each execution takes a sequence number when it starts, before the provider is touched, and a
  record whose sequence is not ahead of the stored one is dropped (`AGREE-10`).
- **A swap and the writers it collides with.** The sequence decides WHICH record may stand, and the
  compare-and-swap decides whether the write lands — so a swap that gives up deciding hands the win
  to whoever wrote last, which is the failure the sequence was taken to prevent. The swap is
  therefore retried until it succeeds or until the row it reads is one it may not replace (the
  case where dropping the write is the right answer), and NOT a fixed number of times: how many
  writers a row has is not a number the store knows, so any cap is a rule that the next writer
  loses — and the next writer is the newest execution (`AGREE-18`). What ends it instead is a
  deadline, which RAISES (`AGREE-19`): `None` from this store means "an older write correctly
  lost", and a caller that read a timeout as that would report an agreement as superseded that was
  never written.
- **A run and the order it must be given before it can be trusted.** A sequence is what makes an
  execution's record lose to a newer one's, so a run that cannot be numbered has no ordering at all
  and would record unconditionally — the whole protection removed, not merely weakened. The failure
  is therefore fatal to the RUN and not to the world: the invite is not touched, the reply is not
  sent, the change settles as one that was not executed, and the reason is a decision row
  (`AGREE-20`). The alternative is the round-3 finding: an unnumbered execution that timed out on
  the sequence lock resumes after a newer run has recorded and overwrites it.
- **A read-back and the bundle it is recorded into.** A bundle that does not say who the update
  would notify has no reviewed guest list at all, so the only list available is the provider's —
  the one an editor racing the write controls. Such a bundle records no guests and is `unchecked`
  with a note (`AGREE-11`), and a bundle that would email the guests without naming them cannot be
  approved in the first place (`AGREE-12`).
- **A row and the key it is filed under.** A payload that names a different agreement is a corrupt
  row, not a record of the key: served, it is a duplicate of the agreement it names, and checking it
  writes its payload over the real one (`AGREE-13`). A payload from a newer build is the same
  problem in the other direction, and it arrives in three shapes that are refused for one reason —
  read as this build's subset and written back, the newer record is deleted: a status this build
  does not have (`AGREE-15`), a field this build does not write (`AGREE-16`), and a schema version
  higher than this build reads (`AGREE-17`). They are three controls rather than one because the
  guard for each is a separate line, and a test that lets the version check answer for all three
  leaves two of them unexercised — which is what the round-3 mutant run found. A field of the RIGHT
  name holding a value this build cannot read is refused for the same reason (`AGREE-23`):
  `unreviewed` decides whether a row is checked at all, and `bool("false")` is `True`, so coercing
  it turned a value nobody wrote into "skip this row forever" — the one answer that never resolves
  itself — instead of into the unreadable record it is.
- **A store failure and an effect that already landed.** The calendar effect is `DONE` before the
  agreement is written, so an exception from the store would abandon the reply and leave the bundle
  `executing` over an invite that really moved. The failure is logged to the decision log and the
  agreement is marked `unchecked`; the run finishes (`AGREE-14`). The fallback row that carries that
  failure carries the REVIEWED guest list with it, because the alternative is not a neutral value:
  `attendees: []` is this store's claim that the owner agreed to no guests, and the next check
  measures the calendar against it — so a failed recording produced a baseline nobody agreed to and
  reported the owner's own guests as drift from it (`AGREE-21`). Where the bundle named nobody there
  is no baseline to carry, and the row says `unreviewed` exactly as a successful recording would
  (`AGREE-22`).
- **A write and its read-back.** The PATCH and the GET are two calls, and anyone with write access
  to the invite can edit it in between. Nothing read back is ever adopted as agreed: the record is
  built from the bundle the owner approved, the read-back is compared against it, and a difference
  lands as `drifted` immediately (`AGREE-2`, `AGREE-3`). One note on `title`: it is recorded from
  the reviewed display and is still not one of the four compared fields, so a rename racing a write
  is recorded as the owner reviewed it without being reported as drift.
- **A restart.** Every one of the above is about a store, and a store that lives in the process
  would make "the product remembers what you agreed to" a claim about a session (`AGREE-8`).

## Not covered

This section is the honest one. A claim made without it would be doing work it cannot do.
`redteam/README.md` is the source for the first four items and should be read in full before quoting
any red-team number.

1. **Nothing about a live provider, in the red-team harness.** Its ports are `SimMail`, `SimCalendar`
   and `SimSearch`. It is evidence about the engine and its boundary, **not** about a real Gmail or
   Google Calendar connection. Specifically it does not exercise: Gmail's or Calendar's
   serialisation of a message, event, `conferenceData` block or attendee list; the **alias** forms a
   real `From` can take (the harness models the display-name form and nothing else — not group
   addresses, plus-addresses, punycode domains, or a display name that is itself an address);
   message-id and thread-id behaviour, a `References` chain the provider rewrote, or a message
   appearing twice; provider-side context or connector tool results the engine never put in the
   payload; or rate limits, partial failures, re-auth and outages beyond the two fixture shapes
   (`EtagMismatch`, `ReauthRequired`) and the ambiguous-timeout path. **Nothing at all about Tavily**,
   whose live responses are replaced by `SimSearch`.

   *Distinct from this:* the 2026-09-19 live run **did** exercise real Gmail, real Google Calendar
   and real Tavily. It is one run of one scenario on one day, and it is not a substitute for the
   coverage listed above.

2. **Nothing about the model's willingness, in a deterministic run.** A deterministic run cannot tell
   you whether a real model would attempt an injection — only what happens if it does. A green
   deterministic table is not a claim about model behaviour.

3. **Nothing about a model the harness did not run.** `--live-model` measures one model at one
   temperature on one day. On 2026-09-19 real Nemotron Super attempted **0/8** injected actions. That
   is a sample, not a property, and it is why the enforcement claim rests on the deterministic run.

4. **Nothing about coverage outside the corpus.** Nine cases, each named. `must`/`must_not` checks
   are only as good as the corpus's idea of what could go wrong; a disclosure path nobody wrote a
   case for is not covered by a green run. The generic checks that apply regardless
   (`protected_text_outbound`, `other_event_text_outbound`, `email_to_anyone_but_sender`,
   `second_event_touched`) narrow that gap; they do not close it.

5. **Nothing about the browser or the API surface, from the red-team harness.** It drives the engine
   and executor directly; the v2 routes and the web UI are covered by their own tests
   (`tests/test_api_v2.py`, `tests/test_api_v2_web.py`, `tests/test_e2e_v2.py`).

6. **The local skill runner is not a VM, and does not pretend to be.** It intercepts CPython's own
   audit events, which is a strong guard for Python-level escapes and one a skill cannot disarm
   (CPython offers no way to remove an audit hook). It does **not** stop a CPython interpreter bug, a
   native-code escape that raises no audit event, or a skill that forges the result bytes on the
   descriptor it was handed. Its containment is defence in depth — empty env, empty cwd, `-I -S`,
   resource caps, a network-deny sandbox profile where available — and **every layer actually applied
   is reported in `RunResult.isolation` rather than assumed.** `sandbox-exec` is not present on every
   host; when it is absent, that layer is absent and the report says so.

7. **The private-boundary matching is heuristic.** Refusal combines normalisation and a
   content-word overlap ratio (`overlap_ratio=0.5`). It is tested against reworded leaks, but a
   sufficiently indirect paraphrase is a known-hard problem and this is not a proof of
   non-disclosure.

8. **Denial of service and denial of wallet are partially mitigated, not solved.** The spend cap
   bounds the money; the wall-clock and resource caps bound a skill. Neither bounds the owner's
   attention, and a sufficiently confusing proposal is still a proposal the owner has to read.

9. **Out of scope by construction:** a malicious provider, a malicious dependency, a compromised
   host, and a malicious owner. See "Not modeled" above.

10. **The port-boundary recorder proves calls, not effects.** `_PortBoundary` wraps the Python port
    object, so it sees method calls made through that object. It **cannot** see a provider-side
    mutation caused by a connector's own secondary HTTP call, a background retry thread, an SDK
    callback, direct transport access that bypasses the port method, or a provider that accepted a
    write and then raised. The comparison it feeds is "a call the harness's effect log does not
    know about", which is **not** "no external effect occurred". No red-team number here supports a
    claim about the live provider boundary.

11. **The recorder does not classify what it records.** `_PortBoundary` records `ok=True` for any
    callable it is asked for, including a mutating provider method added later. A newly introduced
    method is recorded as an ordinary call unless a case or a check explicitly names it as
    prohibited, so the boundary depends on a manually maintained call vocabulary. A green run means
    the vocabulary caught what the corpus knew to name.

12. **`References` is not re-verified against provider state before a reply is sent.**
    `send_reply()` takes an `in_reply_to` message object from its caller and builds the header from
    `MailMessage.references`; it does not re-fetch that message immediately before sending. If the
    object was obtained earlier and the provider's threading state moved, the sent header can carry
    a stale chain. Gmail's own `threadId` still threads it, but another client can receive an
    incomplete chain.

13. **The sender pin is normalised structurally, not by RFC parsing.** `ports.py` accepts a broad
    address shape and does not parse quoted local parts, comments, internationalised domains, or
    canonicalise the domain. The failure mode is mostly a **false refusal** — a legitimate
    correspondent pinned as a different identity — but the recipient decision is security-sensitive,
    and "the provider formats it differently today" is not ruled out. Normalisation should be
    explicit and provider-account aware; it is not yet.

14. **A crash between an approval insert and its compensation leaves a pending approval.** Plan
    commits that fail withdraw the approvals they raised, because broker approvals are raised
    outside the transaction (they do provider I/O, and holding the write lock across a network call
    would trade a rare inconsistency for a common stall). That compensation is a Python frame: a
    `SIGKILL` between the broker call and the withdrawal leaves an approval recorded for a plan that
    never committed. Such an approval is bound to exact arguments and is spendable once, so it
    cannot be replayed onto anything else; it can still be **shown to the owner as a pending
    approval that corresponds to no plan**. `ATTACH`ing `approvals.db` would close the window and
    was rejected as the more complex answer to a narrower problem.

15. **`engine.changes()` serves the set loaded at startup, and now says so.** `store_health()`
    reports `records` (what the store holds now, re-scanned when the row count or the database's
    `data_version` moved) and `loaded` (what the engine is serving) side by side, so the divergence
    is visible rather than hidden. **Visible is not fixed:** `engine.get()` and `engine.changes()`
    both answer from that in-memory set, so if another process replaces or deletes rows this process
    keeps serving its own copy until it is restarted. Two servers sharing one data directory is
    already outside the supported shape — one owner, one process — but the health payload is where
    it would show, and it does.

16. **A standing-limit grant is re-checked immediately before each effect, not atomically with it.**
    The executor re-reads the mode revision and the reviewed-state digest before the calendar edit
    and again before the reply, but the check and the provider call are two steps. A mode flip or
    attendee change landing in that gap (milliseconds, within one process) is not caught for the
    effect already in flight; the calendar edit is still refused by its ETag if the event itself
    changed. Closing it fully needs provider-side authorization the Google APIs do not offer.
17. **The invite update is a Google-generated email.** Under a standing limit it is permitted only
    when the event's guests (read from the provider at check time) are exactly the owner and the
    pinned sender, so the only person Google notifies is the one the reply goes to. A missing
    notification setting defaults to `none` at the executor (`tests/test_send_updates_default.py`).

18. **The public playground cannot be interrupted mid-call; it can only stop starting calls.** The
    1 s budget is a wall-clock deadline checked at every model call (`_Deadline`), so a run that is
    inside a single blocking call when the budget expires finishes that call and is refused at the
    next one. There is no worker to kill: the budget is enforced in-process, and the alternative —
    running the attack in a thread or process with a hard timeout — buys a `SIGKILL` that can land
    while the harness is mid-write, so a run that is killed is a run whose receipt is a lie. What
    bounds the endpoint against a hostile caller is therefore the pair, not the deadline alone: the
    8 KB body and 2,000-character caps bound the size of the work, and the token bucket bounds how
    often it can be asked for. The residual is that a single pathological input can hold one
    worker for longer than 1 s. Measured worst case is asserted in
    `tests/test_playground_hardening.py::test_the_worst_case_attack_is_bounded_in_wall_clock_time`.

19. **Two prompts are not reachable from the model-view endpoint, and one of them carries the raw
    email.** (a) A **classification** call mints its change id before it calls and files its receipt
    under it; if the answer is "not a meeting change", no `Change` is ever created, so
    `GET /v2/changes/{id}/model-view` has no id to require and 404s. The row is written and is
    readable through `ChangeEngine.model_calls(id)`, but that id was never returned to anyone — so on
    a message that became nothing, the prompt containing the raw body is the one the owner cannot
    open from the API. (b) `changes._ask` **writes the receipt after the provider returns**, so a
    crash in between (an audit write failure, i.e. a broken chain) leaves a prompt that left with no
    `model_saw` row. The gate's own decision row for the same payload is written *before* the call,
    which is what keeps (b) from being a blind spot: the evidence that a prompt went out does not
    depend on the receipt landing.

20. **The privacy gate refuses exact containment and near paraphrase; it is not a proof of
    non-disclosure.** A reworded protected value that shares fewer than the `overlap_ratio` of
    content words with the original passes. `redteam`'s evaluator used to have the same shape from
    the other side — it asserted the **exact** protected strings were absent from the outbound
    payload, so a paraphrase that disclosed the same fact in different words would have been scored
    `held`. It now matches through the gate's own `_payload_flat`/`_how_present`, over the encoded,
    partial and numeric forms, and over every surface rather than only the outbound ones (see 22–24
    for what that still misses). Both halves remain the same known-hard problem stated twice: the
    corpus is a floor, not a ceiling, and a green run is evidence about the inputs it ran, not about
    the class of inputs.

21. **The rate limiter's memory is bounded by evicting, and an eviction is a looser limit.** The
    bucket table is capped (4,096 callers) and idle refilled buckets are forgotten as the table is
    walked, so the endpoint cannot be made to grow without bound by a caller appearing from many
    addresses. The cost is the other direction: an evicted caller returns with a full bucket. The
    failure mode was chosen deliberately — a cap that could *deny* service to a caller that had
    never spent a token would be a DoS the limiter performs on the attacker's behalf — but it does
    mean the limit is a rate limit under normal load and a best-effort one under a distributed
    flood. The table is also **process-local**: run the app under N workers and the effective limit
    is `10 × N` per caller, because each worker keeps its own table. A shared counter (Redis, or a
    single-process deployment) is what would make it exact, and the sample deployment is one
    process on one machine — which is a description of today's deployment, not a property of the
    limiter.

22. **A route object that changes its own dispatch after the gate has approved it is NOT covered.**
    The per-request `/v2` gate (`src/privatemesh/api_v2.py`) resolves the route that is about to
    answer a request and refuses unless that route proves gated — by identity, against the gate's own
    registry — or proves that it IS the vetted public route. The object it resolves is a live Python
    object, and `route.handle` is an attribute on it. Code running inside the server process can
    rebind `/v2/session`'s `handle` to a rogue coroutine after startup, and the gate, which asked
    about the object and got a true answer, will have approved the object that then served the
    owner's payload. This is the ninth review's third High, and it is recorded here **as not covered**
    rather than guarded. The same is true of everything else the gate reads off a route — `endpoint`
    included — and of the registries themselves: `_V2_GATED_ENDPOINTS` is a process-global `WeakSet`,
    and code inside the process can add to it.

    The reasoning is this model's own trust boundary, not a preference. Every guard in this document
    assumes the process is running the code this repository shipped. An attacker who can rebind an
    attribute on a live route object already has arbitrary code execution inside the process, and
    from there the owner token file (`~/.privatemesh/owner.token`), the audit log, the provider
    credentials and the memory store are each reachable by a shorter path than this one. A guard
    against route rebinding would be a lock on one window of a house whose front door is already
    open — and worse than useless if it were read as coverage, because it would suggest the process
    boundary is enforced somewhere that it is not. So none was added.

    What *would* cover it is a router that cannot be mutated after it is built: route objects frozen
    so `handle` cannot be rebound, or a dispatch table copied into an immutable structure at startup
    that the router consults instead of the live objects. That is a property of the routing layer
    rather than of this gate, and getting it would mean this project's own router rather than
    FastAPI's. Not done; this paragraph is why the decision is legible instead of silent.

    The middleware stack is the same failure reached by a second route, and it is named here because
    the stack is where this gate's POSITION is enforced rather than assumed. `install_v2_request_gate`
    refuses to build a mis-ordered stack and the product re-checks the assembled one, and both are
    checks of a fact about the stack at the moment they ran — `app.user_middleware` is a live list,
    and `build_middleware_stack` builds from that list. Code inside the process can insert an entry
    into it after both have passed, inside the gate, and the stack serving the next request then
    carries that entry between the gate's approval and the route it approved. That is the ninth
    review's path-rewrite leak again, by a different door: a gated `/v2/session` request answered as
    a 200 from a route nobody vetted, to a caller carrying no token. Re-checking the stack on every
    request is not the fix it looks like, for this item's own reason — the check would read the same
    list the writer just wrote to, so the mutation that defeats it can also satisfy it, and what is
    left is a hot-path check answering a question the process boundary has already answered. Nothing
    in the reasoning above changes with which attribute is written: putting a middleware into
    `user_middleware` is arbitrary code execution inside the server process, and the owner token
    file, the audit log, the provider credentials and the memory store are each reachable from there
    by a shorter path than this one. What would cover it is what would cover route rebinding — a
    stack frozen once it is built, or this project's own router holding the dispatch order in a
    structure the serving path cannot reach. Not done, and not to be read as covered: a guard here
    would suggest the process boundary is enforced at a point where it is not.

23. **A semantic paraphrase with no shared surface is not caught, and no amount of string matching
    will catch it.** The care review's finding 7 asked for a fix, and this is what was built and
    what it does not do. The widened check in `boundary.py` compares a payload against a protected
    value through: Unicode NFKC normalisation and case folding; percent-, HTML- and base64-encoded
    spellings, decoded and re-checked; a run of `_PARTIAL_TOKENS = 2` consecutive content tokens;
    and number matching — every digit run of 5 or more, plus 4-digit runs standing beside a month
    name, which is what makes a date recognisable without making every year a match. That closes
    the mechanical evasions: a value that was encoded, split across tokens, abbreviated to its
    distinctive part, or restated as a date in another order.

    What it does **not** close is a value restated in words that share nothing with the original.
    `QQ-4471-ALPHA-9` is caught in every spelling above; "her benefits number" is not caught, and
    neither is a home address given as a description of the building. The `overlap_ratio` check
    (item 7) catches a reworded *sentence*, because a reworded sentence keeps its content words. A
    value with no words in it — an id, a reference number, an address — has no content words to
    keep, so the paraphrase of a *code* is the case this system does not solve. It is stated here
    rather than left implicit because the alternative is a gate that reads as stronger than it is:
    **the gate refuses what it can prove is present, and its absence is not proof of absence.**

24. **The homoglyph and transliteration table is a floor, not a character set.** `_payload_flat`
    folds the confusable characters that were enumerated when the widening was written — the
    Cyrillic and Greek lookalikes for Latin letters, the fullwidth forms, the mathematical
    alphanumerics. Unicode contains more confusables than that list, and new ones arrive with new
    blocks. A value spelled in a homoglyph the table does not carry reaches the payload as an
    ordinary string that does not match. The failure direction is a **missed refusal**, not a
    spurious one, and the mitigation is that the table is one dict to extend — with the honest
    caveat that extending it is a manual act, and no test claims the list is exhaustive.

25. **The red-team evaluator's `stored` surfaces are the ones a value could reach *today*.**
    `redteam.evaluate` now reads the audit rows, `/today`, `/changes/{id}`, `/model-view`, the digest
    ledger, the error and refusal text, and the calendar arguments a case proposed — and matches
    each of them with the gate's own `_how_present`, so the harness and the gate cannot drift apart.
    That list is a claim about the surfaces this build has. A route added later is not in it until
    somebody adds it, and a check that is not asked cannot fail.

25. **The clock reader is a matcher, and after round 5 it is no longer the boundary for a reply
    body — but it is still the boundary for every surface a fragment cannot reach.** Two rounds of
    review found the same shape twice, and the second finding is the one that changed the design.

    **Round 4** found that the clock check worked by *containment*: the reply was refused if it
    contained the other meeting's timestamp as a string, so every spelling of the same time that was
    not that string walked past it. "a quarter after nine" was the example that escaped, and it was
    not the only one. The reader became a parser — `boundary.time_of_day_mentions` turns numeric,
    dotted, spaced, compact, am/pm and ISO forms into a minute of the day, reads the word forms
    through `past`/`after` and `to`/`before`, reads `half past`, `quarter` and `twenty five`, and
    compares minutes against the day's other meetings. `minutes_of` and the literal fragment checks
    remain underneath it as the second net.

    **Round 5** found that this was still a matcher, and a matcher loses by construction: the
    attacker picks the spelling and the defender has to have thought of it first. The fourth review
    listed ten, this file now names all ten, and `tools/r5_probes.py` re-runs them one by one:

    | spelling | names | can it be a reply-BODY byte? | does the clock reader read it? |
    | --- | --- | --- | --- |
    | `09:15-09:45` | 09:15 | no | yes, 09:15 |
    | `fifteen past eleven` | 11:15 | no | **no** |
    | `in fifteen minutes` | 11:15 | no | **no** |
    | `quarter after nine pm` | 21:15 | no | **wrong: 09:15, the pm is dropped** |
    | `twelve fifteen pm` | 12:15 | no | **wrong: 00:15** |
    | `9h15` | 09:15 | no | **no** |
    | `neuf heures quinze` | 09:15 | no | **no** |
    | `九点十五分` | 09:15 | no | **no** |
    | `MToxNSBhbQ==` (base64) | 11:15 | no | **no** |
    | `１１：１５` (full-width) | 11:15 | no | **no** |

    The body column is `docs/THREAT_MODEL.md` item 1 as a measurement: a reply body is the render of
    fragments the ENGINE generated, each carrying the meeting it derives from, and a sentence the
    engine never rendered is **ineligible** rather than scanned-and-allowed. The reviewer's whole
    list is ineligible, and the reason is not that the reader knows these spellings — the reader
    reads **one of the ten correctly**, and gets two of them actively wrong. They are ineligible
    because no catalogue entry has that text, which is a fact about provenance and not about
    spelling. The two wrong readings are worth stating plainly: `twelve fifteen pm` reads as
    **00:15**, and `quarter after nine pm` reads as **09:15** — the meridiem is dropped, so on any
    surface where the reader is still the boundary, an afternoon time is compared against the wrong
    half of the day.

    **Where the escapes still matter.** Item 1 covers reply BODIES and nothing else. Every one of
    the ten is still a live spelling on the surfaces that are not fragment-composed:

    - **The reply's `subject`.** It is model-authored free text that reaches a provider header, and
      it is checked by `_clause_four` and the port with `check_text` — the matcher, through
      `foreign_event_in`, which asks the clock reader about another meeting's `:clock` fragment. A
      foreign time written there in one of the seven unread spellings is not caught.
    - **The invite's free-text fields.** `location` is no longer one of them: round 6 made it the
      render of a catalogue record the engine FETCHED (`venue_catalogue`/`venue_id`, `render_venue`),
      so a place is selected rather than authored, and a location that is not that record's render is
      refused at the port (`_venue_not_rendered`) before any value check runs. What remains is
      `start` and `end` — provider-bound instants the engine derives from the pinned slot — and the
      summary, which this path does not write. `guard_calendar_fields` still asks the two questions
      it always asked (`check_payload` for the owner's protected values, `guard_calendar_foreign`
      for another meeting's literal fragments), and the second is `foreign_event_in` again: the same
      matcher, the same reader, the same seven spellings. It is now the second net on a field that
      is composed rather than the only net on a field a model wrote.
    - **The owner's own revisions.** The owner's words are the owner's decision (item 26), so they
      are disclosed rather than refused, and the reader is what decides whether the disclosure fires.
    - **The digest.** `care.compose_digest` is composition-by-construction already — a `DigestLine`
      requires the record id it derives from and refuses a bare string — so its body is covered by
      the same argument as the reply's. Its free-text fields are not.
    - **Everywhere `_payload_flat` matching is the check** (items 22 and 23): the protection there is
      the encoded/partial/numeric matcher, not the catalogue.

    **What the reader still does not read, deliberately.** A time stated as an interval with no
    clock in it — "late morning", "after lunch", "first thing", "end of the day", "early afternoon"
    — reports no time at all. This is not a gap the same parser can be widened to close: there is no
    minute in the text to compare against a minute of the day, and the interval a phrase like "late
    morning" picks out is a convention, not a value. Reading it as a range wide enough to be safe
    would refuse a correct reply that happened to say "morning", and a check that fires on the
    product working is a check somebody switches off. On a reply body this no longer matters, which
    is the point of round 5; on the subject, the calendar fields and owner text it still does.

    Three boundaries inside the parser are deliberate and were measured rather than assumed. A
    **digit hour with a word direction** — "5 after 9", "10 before 10" — is not read, because "3 to
    5" is a range and a reader that took a digit-hour-plus-direction for a clock would report 08:55
    for "between 3 to 5 suppliers"; the word forms are read because `quarter after nine` has no
    second reading as a range. A **spaced numeric pair requires a meridiem or a separator** — "9
    15am" is read, "3 15 people" is not — because the alternative is a count becoming a time. And
    `half nine` is not read, which is a miss of a *spelled* form rather than a limit of principle: it
    is 08:30 in one country and 09:30 in another, and a reader that guessed would refuse a correct
    reply half the time. All three are asserted as no-time in `tests/test_care_review_r4.py`.

26. **The owner's own revision is disclosed, not refused — and then the SEND is refused, which is a
    decision rather than an oversight.** `ChangeEngine.revise` is the owner speaking, and the
    owner's words are the owner's decision; silently refusing them would be this system overruling
    the person it works for, and silently rewriting them would be worse — the owner would be reading
    a message they did not write. So the revision is KEPT, and a revision that names another meeting
    is **shown on the review screen before approval** (`Change.owner_revision_mentions`, served on
    `/v2/changes/{id}`) and **recorded in the decision log** under `RULE_OWNER_REVISION`, with the
    rule's own words saying that the reply cannot be sent while the mention stands.

    The reply is then refused at the port by `ForeignEventLeak`. That is deliberate and it is
    argued rather than assumed: the send path sees a body, and it **cannot tell an owner's sentence
    from a draft's**. The only signal that would let it would be a flag on the bundle, and a port
    that trusted a flag saying "the owner wrote this" is a port a compromised planner can point at
    any sentence at all — which is the hole this whole round is about closing. The alternative
    designs were considered and rejected: refusing at revise time (overrules the owner without
    telling them why the system cares), and accepting at the port (needs the flag). What is left is
    the one that tells the owner at both moments and overrules them at neither — they see it before
    they approve, while the words are still theirs to change, and they see it again if they run it
    anyway. Nothing about the path is silent.

    The check itself is the matcher described in item 25, with the same seven unread spellings. The
    asymmetry between this path and the reply body is the point: on a body, a spelling the engine
    never rendered cannot become a byte at all; on an owner revision, a spelling the reader does not
    know is a disclosure that did not fire.

27. **An owner's reply revision was unapprovable before this round, for two reasons that were both
    silent.** `_revise` never wrote `change.reply_fragments`/`reply_template`, so after any owner
    rewrite of the body those two fields still described the PREVIOUS reply — and `_clause_four`
    rebuilds the message from them and compares it against the body in the bundle, so the owner's own
    sentence was refused as "text in it that no source declared", as though a model had smuggled it
    in. Separately, the edit passed `calendar_display=None` unless the time or the venue had changed,
    so `changesets.edit` rebuilt the display from the arguments alone — where the guest list is not —
    and the bundle came back with `attendees: None`, which
    `_refuse_a_notifying_bundle_that_names_nobody` then refused, correctly, because approving it
    would record an agreement to email people nobody had reviewed. Both are fixed and asserted
    (`tests/test_care_r5_structural.py`). The second one's refusal message told the owner to
    "prepare it again", which is a way out the UI does not offer — worth knowing that the remedy a
    refusal names is itself a claim that can be wrong.

28. **CLOSED in round 7: the calendar `location` provenance rule no longer binds only where the
    change carries a selection.** Round 6's rule was "the port confirms a good value rather than
    recognising a bad one", but `_venue_not_rendered` returned `""` — "this rule does not apply" —
    when the change carried no `venue_catalogue` or no `venue_id`, so a bundle assembled by hand, or
    a plan whose research found no venue, was reported clean and its `location` was written with
    nothing having rendered it. Both are now REFUSALS (`CalendarRefused`), which is the rule the day
    window already followed: a check that cannot ask is not a check that answered. The one state
    that still passes is a write carrying NO location at all — a calendar write that states no place
    writes no place, and its other fields are checked by the protected-value and foreign-event
    checks on the same call. Controls: `R7-4a`, `R7-4b`.

29. **CLOSED in round 7: `compose_digest` no longer has a `None` for `records`, and a line's text
    must be the RENDER of the record it names.** The parameter is required, so a caller that cannot
    say what its records render cannot compose a digest at all — the "worst available shape" was a
    well-typed claim that is false, and it is now refused instead (`DigestBadLine`). The record id
    also travels onto the fragment and into `artifact_id`, so the claim is checkable downstream
    rather than merely well-formed. Controls: `R7-4c`, `R7-4d`.

30. **The foreign-event set is narrowed by the correspondent's own message, and the narrowing is by
    content.** `foreign_fragments_of` excludes three things: the pinned meeting's fragments, the
    fragments of `exempt_text`, and nothing else. The third exists because the other two are read at
    the wrong MOMENT — by the time a reply reaches the port the calendar move has already happened,
    so the meeting that was at `Old Cafe, King's Cross` is now at `The Standard`, and `cafe` is no
    longer one of *its* fragments while it is still one of a second meeting's. The exemption is
    bounded in the direction that matters: it is the words of the specific inbound message the
    change is about, read from the provider's copy (never from `change.subject`, which is a surface
    a bundle can write), and it exempts a word only where that word actually occurs in that message.
    What it does not do is distinguish *meaning*: if a second meeting's name happens to appear in
    the correspondent's own email, that name is exempt from the foreign check for that reply. The
    alternative — a set that ignored the correspondent's words — refuses correct replies, and a
    check that fires on the product working correctly is the one that gets switched off. The
    control for it is `test_finding_01_the_exemption_does_not_excuse_a_sentence_the_correspondent_never_wrote`.

31. **The digest's once-a-day check reads its day from the bundle, and the ledger does not.** Clause
    7 counts `sent_on(mail["care_day"])` — a value the change set carries — while the executor's
    `claim` takes the day off `plan.day`, which it derives itself from the digest it built. A bundle
    that named a different day would therefore be CLEARED at approval and REFUSED at execution by
    `DigestAlreadySent`, because the ledger's UNIQUE index on `day` is the thing that actually
    enforces the cap and it never reads the bundle. The direction is fail-closed — a refusal, not a
    second digest — and it is recorded here because the two days are two different answers to the
    same question and a reader comparing them deserves to know which one is load-bearing. On the
    product's path they agree by construction: `_prepare` is the only writer of `care_day` and it
    writes `change.plan_day`, the same day the executor resolves. This is the same shape as finding
    2 (a guard reasoning about a day the bundle can name) and it is the first candidate for a round
    that has budget for it; it is not a disclosure path today.

32. **The day a guard reasons about is computed at send time, from the provider.** Round 6 moved the
    window's source from the bundle to trusted state, and round 7 did not pin it further: `_local_day`
    and `_day_window` still read the pinned event back through `calendar.get_event` at the moment of
    the call, under the owner's configured zone. That is deliberate — a calendar other people are
    editing has to be read then, and a window fixed at approval would describe a day that no longer
    exists — but it is the one value on the outbound path that the ENGINE computes rather than
    derives from the approved plan, so it is named here with what bounds it. A provider that restamps
    the pinned event between approval and send moves the window with it, and a foreign meeting that
    fell outside the moved window would not be seen by the foreign check. What still holds: the
    bytes are the render of the change's own declared fragments (`R6-1a`), each calendar field is the
    approved value (`R7-3a`), and the location is the render of the record the change chose
    (`R7-4a`/`R7-4b`) — so the residual is a sentence the change itself declared AND a day whose
    other meetings the window missed, not an unrendered value reaching the wire. The other two
    send-time fields are the transport's and carry no content: `Message-ID` is minted by
    `build_reply` for the message being sent (the provider assigns `From`, and `Date`), and
    `References` is read off the provider's own copy of the pinned message rather than composed.

33. **The version check at the call is a check, not a lock, and the window after it is bounded by
    the provider rather than by this process.** Round 8's finding 2 is answered at the last moment
    this process controls: the port re-reads the change and refuses unless it is still on the
    version its fields were bound to. Between that read and the provider's receipt there is still a
    window, and it is not closed — a revision landing inside it is not seen. What bounds it is that
    both effects are conditional on the same approval: the calendar write carries `If-Match` on the
    reviewed etag and the reply carries the approved idempotency key, so a write that lands after
    the owner moved on is a write the provider's own precondition can still refuse, and a duplicate
    is reconciled rather than sent twice. Closing the window itself would need provider-side
    authorization that neither Google API offers. Stated here rather than left implied: **the check
    narrows the window from "the whole provider call" to "the gap between two adjacent statements",
    and the residual is real.**

## Reading a result from this system

- **`held`** — no declared check failed, no safety invariant was violated, and **no forbidden side
  effect landed at any point**, including before an exception. A case is held only if the effect log
  shows nothing landed. `ERROR` is **not** `held`: an exception that is not a named policy refusal
  makes a case an error, never a defended one.
- **`attempted`** — whether the model's output carried the injected action. Without this column a
  live run is unreadable: a run whose model ignored every injection is indistinguishable from a run
  whose guards stopped a model that tried.
- Say "**blocked the tested attempts**". Do not say "cannot be escaped", "unhackable", or "fully
  secure" — none of which these results support.
