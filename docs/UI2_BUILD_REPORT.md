# UI v2 build report — 18 September 2026

Implemented in `web/` on `build/ui2`. The previous chat/receipt interface has been removed.
This is a sample-backed implementation, **not completed release acceptance**.

## Delivered

- Screens 0–8: public prepared-plan preview, connections/private boundary, Today, preparation,
  proposal/editing, exact two-action review, independent outcomes, provenance, preferences and plans.
- The approved light palette, Manrope headings, protected-line/buffer schedule, desktop columns,
  tablet rail and phone layout. Source Sans 3 CSS is configured but its asset remains unavailable.
- Canonical synthetic Alex email; 11:00 original meeting; 15:00–15:45 proposal; protected 16:30 with
  45-minute buffer; closed venue and 16:00 rejection; explicit seating uncertainty.
- Versioned edits and stale/expired refusal; actual changed original calendar in the stale fixture;
  partial/unknown outcomes without automatic resend; offline retained-data notice; empty/no-fit,
  cancelled, paused, missing/authorizing/expired connection and long-wait states.
- One typed sample/live client. Sample mode makes no provider calls. The public illustration remains
  synthetic in both modes. Live ownership must be explicitly true before approval is enabled.
- Source/constraint evidence, independent calendar attendees, scoped consented preference use,
  preference edit/revoke, exact outgoing-field manifests, optional provider IDs/audit hashes,
  and attempted-versus-confirmed model-record distinctions.
- Browser acceptance harness and gitignored `web/.shots/` destination.

## Contract additions

`docs/API_V2.md` documents owner authentication, boundary setup, connection status operations,
nullable pre-proposal data, blocked reasons, full mutation responses, checked venue alternatives,
independent calendar attendee lists, synthetic/null evidence URLs, typed errors, preference PATCH,
structured plans/provenance and optional individual model-call records. Engine lanes must implement
these additions before live integration acceptance. Public preview is a local synthetic client method,
not a new backend endpoint or an inference request.

## Verification and limits

- Production `npm run build`: PASS. `npm test`: PASS, 34 tests in two files.
- `playwright test --list`: PASS, 16 cases across desktop and phone. `git diff --check`: PASS.
- Vitest includes rendered React hero, stale/expired recovery, partial/unknown, boundary saving,
  repeat edits/timezone, venue address, cancellation, preferences, offline, terminal reload and invalid expiry.
- Sample transport tests assert zero fetch calls and exercise timed stages, refusal and provenance.
- Playwright contains both requested viewports, screens 0–8, exceptional states, keyboard navigation,
  320×568 reflow and a 200%-equivalent CSS viewport. Test discovery works.
- **Browser acceptance is blocked.** Starting Next fails with `listen EPERM`; standalone Chromium
  fails with `bootstrap_check_in … Permission denied (1100)`. No browser screenshots were produced.
  The `.shots/` directory is intentionally empty. No visual, real-device, rendered contrast or native
  browser-zoom acceptance is claimed. The public preview is the real rendered Day component, not a
  captured bitmap; a capture can replace it after browser acceptance is available.
- **Source Sans 3 is missing.** Local font/cache/application searches found no usable asset; the
  install command fails resolving `raw.githubusercontent.com`. Manrope and its SIL license are bundled.
  `npm run fonts` fetches Source Sans 3 and its SIL license for self-hosting when network access is available.
  Until then, the body uses its system fallback. The browser font check intentionally requires both fonts.
- No real provider calls, account connections, messages, pushes, deployment or publication occurred.

## Design-owner decision: VR

**Cut VR from this submission.** Approving the same plan in a headset adds another surface, input
model and filming transition without improving the job: handling an email-driven schedule change
while protecting a private commitment. A spatial day view makes the boundary harder to inspect and
captures poorly inside a short, mostly flat film. My judgment is that it dilutes both the film and the
Design criterion; it does not raise our chance of placing enough to justify the work.

Build order stays: merge API integration → prove controlled real effects and stale refusal → finish
phone/desktop visual acceptance → film the coherent handoff. The existing Quest and VR work do not
change that priority. Reconsider a headset surface after the event only if actual users need to review
plans while already working in VR; it is not part of this build order.

## Commit boundary

The requested targeted `git add` was attempted. Git refused to create the worktree's
`.git/worktrees/privatemesh-ui2/index.lock` with `Operation not permitted`. No commit was created.
The worktree remains on `build/ui2` with the implementation changes available for inspection. On an
environment permitting the worktree index, commit after the remaining acceptance checks. No push.

The remaining unblock is an execution environment that permits font download, local browser/server
launch and this worktree’s Git metadata writes. Then run `npm run fonts`, `npm run verify`, inspect the
captured screenshots, and make the requested targeted commit. No provider keys are needed for that.

## Independent review

Source and rendered-component reviews caught and drove fixes for stale approval content, field/timezone
consistency after edits, outcome/selection races, unverified calendar confirmation, actual attendees,
provenance counts, offline detection and focus loss. Component acceptance passes; the overall design
acceptance remains failed pending the missing font and actual browser/screenshot inspection.

Dependency installation could not finish over the restricted network. The matching existing local
web dependency tree was used for these build/test runs; a clean networked `npm ci` remains part of the
unrestricted acceptance run.

## Round 2 — Concept D: Teach it once. Keep control.

Written in this round; **not build-, test-, or browser-verified in this session**. The verification
claims earlier in this document describe the previous round only. Per manager instruction, no fonts,
network calls, servers, browsers, builds, test commands, git mutations or commits were attempted.

### Product and design

- Keep the current light workspace, Manrope headings and Source Sans 3 body. Tokens remain canvas
  `#F3F6FA`, ink `#18263A`, action `#2457C5`, protected `#704285`, completion `#19664D`, white surface.
  The signature device is still the protected boundary; D extends it into the permission's “Never”
  column and overreach record. No new decorative visual system or generated assets.
- Add a task-level learning path via **Teach this routine**; retain **Handle this change** for the
  existing one-off planning path. This is deliberate compatibility, not automatic installation.
  Human progress reads Learning your routine → Checked → Ready for your review → Done; failed and
  partial states keep their explicit descriptions and never claim Done.
- A readable native disclosure shows v1 stopped, the named private-context violation, the reviewer's
  reason, then v2 tests, held-out checks and review. Synthetic reviewer evidence is explicitly labelled.
- **Review skill permissions** opens a focused permission sheet in the task workspace: Reads / May
  propose / Never, expiry and full wrapping identity. It replaces the task details and day rail while
  open. Desktop uses three columns; phone stacks the same content with normal document scrolling.
  “One screen” means one permission surface, not clipped content or a forced viewport-height fit.
- **Install this skill** approves only the displayed identity. It neither prepares nor executes
  effects. A separate **Prepare with this skill** leads to the unchanged exact-two-action review.
- Skills live in **Preferences** with purpose, version, learned date, use count, last use, identity,
  active/revoked state and named revoke control. Phone access stays Settings → Preferences.
- **Open a later sample change** retains the installed skill and prior plan. Preparing that request
  shows “Using your skill: Meeting option evaluator (learned …)” without a learning attempt.
- “Why this plan?” and the outcome show supplied overreach events as “Stopped at your boundary”,
  including the attempted access and whether runtime or manifest blocked it.

### Protected-time regression decision

**The UI is responsible; the test's expectation is correct.** 16:00 plus the meeting duration cannot
fit before the protected block and buffer. Keep the desktop/phone assertion that role=alert contains
“protected”. The form now reads `meeting_time` from submitted FormData, uses explicit app validation
instead of silent native constraint bubbles (`noValidate`), validates required time/reply, and announces
a boundary conflict before mutation. Valid edits still go through API revalidation. Submission has an
explicit button type. A regression checks that a refused edit preserves the proposal version and input.
The exact original browser failure mechanism was **not reproduced** here; the manager must verify that
this fixes the reported Playwright failure rather than treating source reasoning as browser proof.

### First 30 seconds — revised film storyboard

| Time | Screen / action | Spoken or on-screen story |
|---|---|---|
| 0–4s | Alex's selected request and the plum 16:30 protected line. Click Teach this routine. | “Plans change. Your boundaries stay.” |
| 4–8s | Learning your routine. Open attempt disclosure as v1 stops. Hold on the named violation and reviewer reason. | “The first attempt asked for private details. It was stopped.” |
| 8–12s | Checked, then v2 passed: tests, held-out checks, review. | “The revision works with only what you permitted.” |
| 12–18s | Ready for your review → permission sheet. Read across Reads / May propose / Never; keep expiry and identity visible. Click Install this skill. | “Teach it once. Installing a skill doesn't approve an action.” |
| 18–23s | Prepare with this skill; exact invite and reply review. Click Approve these 2 actions; per-effect confirmation → Done. | “You still decide exactly what changes.” |
| 23–27s | Open the later request. Prepare; hold the reuse line and learned date. | “Next time, it uses what you taught.” |
| 27–30s | Preferences → skill library. Revoke, hold revoked state. | “And you can take that permission back.” |

Use real application captures with the synthetic label retained. Cuts may compress waiting, but must
not turn a simulated test/reviewer/overreach into a live-engine claim. Keep the permission hold readable;
if 30 seconds is too tight in the capture, shorten narration and transitions, not the permission text.
No VR, fabricated provider confirmations, or new assets are needed.

### Manager acceptance handoff

New source: `web/components/Learning.tsx`, `web/lib/sample-learning.ts`. Updated UI, CSS, typed API,
sample adapter, component/transport tests and Playwright coverage. New tests cover exact-identity
refusal, expiry/discard refusal, no effects on installation, two-change reuse, revoke exclusion, encoded
live routes, permission focus, overreach fixture history, and unchanged version after invalid time edits.
The Playwright case records D-attempts, D-permission, D-reuse and D-library-revoked for both projects.

Manager: run the requested fonts/build/Vitest/Playwright commands; inspect the permission page at
320px, tablet and desktop, full identity wrapping, keyboard focus after install/discard, all previous
acceptance cases and the newly captured D screens. Source review cannot certify those checks.
Integration gaps and proposed optional fields are listed in `docs/API_V2.md`; coordinate those with
build/learn before live acceptance. Sample execution and learning evidence remain synthetic throughout.

Independent read-only source review found and drove three additional fixes: save learning_id into
parent state before navigation, retain learning records by ID, and load existing preferences even when
the new skills endpoint fails. Follow-up source verdict: no remaining definite blockers identified.
Regression tests cover all three; they remain unexecuted. Permission exit now restores focus to the
learning heading. This source verdict is not a build, runtime, visual, or release approval.
