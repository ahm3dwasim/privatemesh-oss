# Design lane report, round 3 — from a staged demo to a product, again

Worktree `design3`, branch `build/design3`, from main `32bc40b` (pass 1+2 merged). Everything below
is in `web/`, except this file.

## Why this round existed

A different blind judge scored pass 2 **Design 3/5** — the same number as pass 1, on a fresh vendor
who had not seen either earlier round: "visually coherent and substantially designed, but the
experience still reads as a sophisticated interactive prototype." Its full table is
`design_critic_r2.log`. The manager set eight priorities, in order, covering
every row in that table; this round is that direction, executed.

The two largest changes: **one truthful state machine, everywhere** (no page says both "done" and
"nothing was sent" about the same effect any more), and **Today is now two lists, not one** — Needs
you, expanded, above Recent activity, compacted and grouped by day.

---

## What changed, place by place

**Feed and state** (`lib/feed.ts`) — rebuilt around one rule: *one row per meeting.*

- A refusal that happened while handling a change used to be its own row, sitting apart from the
  change it belongs to. It is now a note carried ON the change's own row (`FeedItem.blocked`) — the
  fix for two Alex rows that read as duplicate history when they were one story.
- `FeedTone` lost `refused`/`attention` as separate chip words; every stage now resolves to exactly
  one of three (`needs_you` / `done` / `blocked`), with the nuance moved into `detail`.
- `dayHeading()` / `groupByDay()` turn a flat, sorted list into `Today` / `Yesterday` / weekday
  groups — the structure "Recent activity" renders directly.

**Today** (`components/TodayFeed.tsx`, `app/page.tsx`) —

- The feed is two sections: **Needs you**, every card expanded, and **Recent activity**, grouped
  under day headings and drawn compact (`.feed-row.compact`, one line, no detail, no refusal note).
  A week of settled history no longer outweighs the one thing still open.
- "Explore sample situations" is no longer a footer on every page. It is one launcher, in a new
  **Demo** tab in Settings (shown only in sample mode) — the single place the product admits it is a
  demo, once, instead of on every screen's own footer.
- First run gets a real exit: `setupDismissed` + a **"Start using PrivateMesh"** button that lands on
  Today, instead of a checklist that just goes quiet once every step reads as done.

**Change detail** (`components/ChangeDetail.tsx`) — decision first, proof on demand, now with no
recursive review step and no tense contradiction.

- **No separate "review" click.** The exact calendar diff and the exact reply are shown inside a new
  **"Proposed change"** card, directly under the summary, before the one button that approves them.
  Pressing it applies to whatever version is on screen — reading this far already was the review.
- **"Left out of this plan"** lists every rejected option with its reason, next to what the plan
  actually does, instead of only inside the collapsed audit.
- **One truthful tense.** `effectLabel` always reads past tense ("Calendar updated", "Reply sent").
  Sample mode says **"Simulated"** once, as a badge at the top of the receipt, and nothing below it
  says "would" — the fix for a card that said "Meeting moved" above "Would update the invite" on the
  same screen.
- Sample mode's model column is retitled **"Stored sample proposal"** (provenance chip: "Sample data
  · untrusted") instead of "What the model proposed" — sample mode never calls a model, so the old
  title was a contradiction a reader hit before reaching the panel that explained it.
- **"This does" → "Proposed change"; "Blocked" → "Refused"** in the decision summary — user language,
  not implementation labels.
- The action group is sized to its own content (`max-width: 520px`), not stretched to the card's full
  width, for three simple choices.
- **"Teach it once"** moved into a collapsed disclosure (`.teach-fold`) beneath the plan, so it no
  longer competes with the approve/edit/leave decision for primary weight.
- **Mobile: the three columns become three tabs.** At ≤1180px, "How this was checked" renders a
  CSS-only segmented control (`:has()`, no script) — Request / Proposal / Allowed — instead of three
  panels stacked one after another down an already-long page.

**Agreements** (`components/Agreements.tsx`) —

- Changed agreements lead; unchecked follow, each with the one reason it hasn't been checked ("This
  is not a problem — press Check now…") and a primary **Check now** action; healthy agreements
  collapse into one-line rows behind `<details>` naming the count — the fix for full cards making
  "still matches" read as loud as "changed".
- Context-specific actions replace the one generic "Open the change it came from": **"Review this
  change"** on a drifted card, **"View original approval"** on an unchecked or holding one.

**Skills** (`components/Skills.tsx`, `lib/sample.ts`) —

- Page renamed **"Learned scheduling rules"**.
- The card's `<h3>` is now the skill's **name**, not its rule text — the fix for two cards that showed
  identical rule text as their only heading. The seeded skill and the sample's four teachable rules
  now each get a distinct human name (`ruleNames` map: "Late-afternoon cutoff", "No early mornings",
  …) instead of "Taught rule 1/2/3".

**Limits** (`components/StandingLimits.tsx`) —

- **Three plain-language principles** ("Only this meeting, only a time that fits" / "Only the person
  who wrote in is told" / "At most one edit and one reply") replace six equally-weighted cards. The
  six exact clauses sit in one `<details className="clauses-exact-panel">`, collapsed, each clause's
  "Exact wording" heading gone (it was the same three words repeated six times).
- Mode changes now surface a toast-styled confirmation (`.saved-note.toast`), not a line easy to miss
  at the end of the page.

**First run** (`components/FirstRun.tsx`) —

- **Explicit progress**: `Step N of 3` plus a fill bar, computed from real state (`chosen`, `taught`,
  connection count) — not a step that ticks itself off before the owner acts.
- Step 1's copy states what connecting *would* let the assistant do, instead of "nothing to do here".
- A real ending: the completion card now has a **"Start using PrivateMesh"** button.
- The nav's Today slot reads **"Setup"** while first run is open (`Shell`'s `firstRun` prop), so the
  highlighted destination matches what's on screen.

**Global** (`app/globals.css`) —

- Dark-mode `--secondary` lightened and its size bumped one step under `@media (prefers-color-scheme:
  dark)` — the two together close the contrast gap the judge measured in audit details and status
  explanations.
- `--tabbar-h` is now a named constant the canvas padding and the sticky decision bar both read, with
  a verified 10px clearance on top of it — checked for real by a new Playwright assertion, not implied
  by two numbers that happened to match.

---

## Every row in the critic's table

Read from `design_critic_r2.log`, not summarised from memory, in the order the table printed them.
"Fixed" means the described problem no longer reproduces; "Addressed differently" took a different
route to the same complaint; "Partially addressed" and "Not done" say why, in one line each.

| # | Screenshot(s) | Finding | Outcome |
|---|---|---|---|
| 1 | All | Persistent "Demo mode" framing | **Addressed differently.** The one quiet header indicator stays (direction 2's r2 decision), but the thing that made every page announce a demo — "Explore sample situations" repeated on every footer — is gone; see #2. |
| 2 | Most | "Explore sample situations" on every footer | **Fixed.** One launcher, in Settings → Demo (sample mode only). |
| 3 | 14-first-run, 06-outcome | Explicit "nothing connected/sent" statements | **Fixed.** First run's step 1 says what connecting *would* do; the outcome receipt says "Simulated" once and reads past tense below it. |
| 4 | 01-today (both) | Feed is a wall of similarly shaped cards, weak prioritization | **Fixed.** Needs you / Recent activity split; Recent is compact and grouped by day. |
| 5 | 01-today, 07-today-after | Status taxonomy excessive (6 words) | **Fixed.** Three statuses only — `needs_you` / `done` / `blocked` — with the sub-reason in `detail`. |
| 6 | 01-today, 07-today-after | Multiple Alex records read as duplicated history | **Fixed.** One row per change; a refusal rides on its own change's row (`FeedItem.blocked`). |
| 7 | 01-today-1280 | Calendar detached from the request needing a decision | **Not done.** The compact diff line inside the decision card gives immediate context without the calendar; a hover-to-highlight interaction between the feed and the calendar was judged lower-leverage than the feed/decision rebuild for this round's remaining scope. |
| 8 | 01-today-375 | Calendar/boundary panel sits after the entire feed on mobile | **Not done.** `today-grid` still stacks the aside after the feed at ≤1023px; unchanged from r2 for the same reason as #7 — carried forward, not newly introduced. |
| 9 | 01-today-375 | Bottom nav visibly crosses card content | **Fixed.** `--tabbar-h` is now a named constant both the canvas padding and the sticky decision bar read, with a verified clearance; proven by a new Playwright assertion (`design-shots.spec.ts`'s "bottom nav never overlaps content at 375×812") that scrolls every screen to its end and measures the real boxes. |
| 10 | 01-today-375 | Phone page excessively long | **Improved, not eliminated.** Recent activity is now compact rows; Needs You cards stay full size because they are the thing the page exists to surface. |
| 11 | 01-today-375, 03a-375 | Bottom nav reads as mid-content, not a stable edge | **Fixed.** Same change as #9. |
| 12 | 03a (both) | "This does"/"Blocked" read as implementation labels | **Fixed.** Renamed "Proposed change" / "Refused". |
| 13 | 03a-1280 | Action container disproportionately wide for three choices | **Fixed.** `.action-group { max-width: 520px }`. |
| 14 | 03a (both) | "Review calendar update and reply" is a recursive second review step | **Fixed.** No review click exists any more; the exact diff and reply are shown before the one Approve button. |
| 15 | 03a-375 | Old/new time comparison separated across blocks on mobile | **Fixed.** The compact diff sits directly under "Proposed change"'s own title, one component. |
| 16 | 03a (both) | "Teach it once" competes with the urgent decision | **Fixed.** Moved into a collapsed `<details className="teach-fold">` below the plan. |
| 17 | 03b, 06 | Three-column view extremely dense at width | **Fixed.** ≤1180px renders the three columns as three tabs (CSS `:has()`, no script) instead of three stacked panels. |
| 18 | 03b, 06 | Tiny arrows don't communicate transformation | **Addressed differently.** The desktop arrows are unchanged (out of this round's scope), but the mobile tab labels ("1. Request", "2. Proposal", "3. Allowed") replace the need to read an arrow at all on the width where the complaint was sharpest. |
| 19 | 03b, 06 | Dashed borders mean several different things | **Not done.** No pass this round gave dashed borders a single reserved meaning; judged lower-leverage than the eight numbered priorities for the remaining time. |
| 20 | 06-outcome | "Meeting moved" vs. "Would update the invite" contradiction | **Fixed.** `effectLabel` is always past tense; "Simulated" is the one place sample mode admits itself, once, at the top. |
| 21 | 06-outcome | "Attendees notified"/"reply sent" vs. "nothing reached a calendar or inbox" | **Fixed.** Same change as #20 — there is no second sentence contradicting the receipt any more. |
| 22 | 06-outcome | "What the model proposed" coexists with "no model was called" | **Fixed.** Sample mode's column reads "Stored sample proposal" / "Sample data · untrusted". |
| 23 | 06-outcome | Large audit apparatus follows the outcome | **Addressed differently** (carried from r2): the audit stays behind the collapsed "How this was checked" disclosure; the outcome card itself is unchanged in size this round. |
| 24 | 07-today-after | Two apparently completed Alex changes remain visible | **Fixed.** One thread per meeting; the "after" state shows one canonical row, latest status only. |
| 25 | 08-agreements | Cards repetitive and oversized; unchanged records same weight as the alert | **Fixed.** Changed leads, unchecked follows with a reason, healthy agreements collapse into one-line rows behind a count. |
| 26 | 08-agreements | "Open the change it came from" nonsensical on healthy/unchecked records | **Fixed.** Context-specific labels: "Review this change" (drifted), "View original approval" (unchecked/holding). |
| 27 | 08-agreements | Two never-checked entries make monitoring look unfinished | **Fixed.** Each unchecked card explains why and offers "Check now" as the primary action. |
| 28 | 08-agreements | Altered agreement offers no next action beyond opening the source | **Fixed.** "Review this change" primary button added to the drifted card. |
| 29 | 11-skills | Two active skills show identical title/rule/example — reads as a bug | **Fixed.** The card's heading is the skill's distinct name; the rule text sits underneath, verbatim. |
| 30 | 11-skills | "Nothing after 15:30" repeats across input/chip/both cards | **Partially addressed.** The literal rule text still appears wherever it is the accurate record of what the owner typed — that repetition is honest, not a bug — but the two cards showing it no longer share an identical *title*, which was the part that read as duplicated data. |
| 31 | 11-skills | "Your skills" vague relative to the actual concept | **Fixed.** Renamed "Learned scheduling rules". |
| 32 | 11-skills | Worked example duplicated, dominates hierarchy | **Not done.** `Narrowing` still renders inline and expanded on every active card; collapsing it behind a one-line summary was judged lower-leverage than the title fix (#29) for this round's remaining scope. |
| 33 | 12-limits | No visible Save/Apply for a mode change | **Fixed.** `.saved-note.toast` — an explicit, styled confirmation on every mode change (the "Confirm mode change" step itself predates this round; see pass-2 report #35). |
| 34 | 12-limits | "The six promises" reads as a legalistic wall | **Fixed.** Three plain-language principles lead; the six clauses sit in one collapsed panel. |
| 35 | 12-limits | "Exact wording" repeats on every clause, adding noise | **Fixed.** One `<details>` panel holds all six; the per-clause heading is gone. |
| 36 | 14-first-run | "Nothing to do" step one, no real connect path | **Fixed.** Copy states what connecting would let the assistant read/do; live mode already offered the real connect action. |
| 37 | 14-first-run | No progress indicator or completion condition | **Fixed.** `Step N of 3` plus a fill bar, plus a final "Start using PrivateMesh" action. |
| 38 | 14-first-run | Nav highlights Today while content is onboarding | **Fixed.** The nav's Today slot reads "Setup" during first run. |
| 39 | All dark | Secondary/metadata text low contrast | **Fixed.** Lighter `--secondary` token plus a one-step size bump under dark mode. |
| 40 | Screenshot set | Only Today and the decision page have phone layouts | **Fixed.** `design-shots.spec.ts` captures all 16 screens at 375 and 1280, light and dark (64 images) — Agreements, Skills, Limits and the three-column audit (now tabs) included. |

**Three changes with the highest scoring impact** (the critic's own list): one truthful end-to-end
state transition, no "would"/"sent"/"moved"/"simulation" contradictions — done (the effect-label and
"Simulated"-badge rewrite, #20–22). Replacing the demo-fixture feeling — done for the repeated
footer (#2) and duplicate skills/records (#6, #29); the persistent header indicator itself was a
deliberate r2 decision, unchanged. Rebuilding the information hierarchy for action-first use — done
for Today (#4–6, #9–11) and the decision page (#12–17); the calendar/aside redesign (#7–8) and the
skills worked-example disclosure (#32) are the next places to spend a design pass.

---

## What I deliberately did not do

- **The calendar/boundary aside still follows the entire feed on mobile** (findings #7, #8). Making
  a request card highlight its own affected calendar blocks, or moving a compact calendar preview
  above the fold on a phone, is a real redesign of `Day.tsx` and the `today-grid` layout — larger
  than the eight numbered priorities left room for this round, and neither finding was on the
  manager's own priority list.
- **Dashed borders still carry more than one meaning** (finding #19) — unsafe input, informational
  columns, teaching modules and unchecked items all still use the same visual grammar. Reserving it
  to one state was not one of the eight priorities and was judged lower-leverage than the items that
  were.
- **The worked example in Skills is still shown inline and expanded on every card** (finding #32).
  The higher-leverage half of that finding — cards reading as duplicate seeded data — is fixed by
  giving each card its own name; collapsing `Narrowing` behind a summary is left for a future pass.
- **The desktop three-column arrows are unchanged** (finding #18, desktop half). The mobile tab
  rebuild answers the complaint at the width it was sharpest; a desktop flow-header redraw was not
  attempted.
- Per the r1 and r2 reports, `src/`, `docs/API_V2.md`, the red-team corpus and the Python tests were
  not touched, beyond this lane's own `.venv` (gitignored) for the live suite.

---

## Proof

| Check | Result |
|---|---|
| `npx tsc --noEmit` | clean |
| `npm test` (Vitest) | **84 passed**, 6 files |
| `npx playwright test --project=desktop --project=phone` (sample) | **54 passed** (27 desktop + 27 phone; 375 and 1280, light and dark), including the new nav-overlap assertion at 375×812 |
| `PM_STATIC_EXPORT=1 NEXT_PUBLIC_API_MODE=sample npx next build` | exports; `out/index.html` written |
| Contrast AA, measured on the rendered page | every screen, both themes, 0 failures |
| Target size ≥24×24 at 375 | 0 failures |
| Horizontal overflow at 375 and 1280 | 0, asserted at every screenshot |
| Bottom nav vs. last card at 375×812 | 0 overlaps, measured (not inferred) across Today, Change (all three "How this was checked" tabs expanded), Agreements, Skills, Limits and First run |
| Screenshots | `web/design-shots/` (untracked) — 16 screens × {375, 1280} × {light, dark} = 64 |
| Live suite (`privatemesh serve --v2 --sample`, own `.venv`) | **2 passed** (desktop + phone) — the judges' path, driven end to end against the real server |

### Running the live suite

```sh
cd <this worktree's checkout root>
python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
cd web && PM_LIVE_API_PORT=8841 PM_LIVE_WEB_PORT=3141 \
  npx playwright test --config playwright.live.config.ts --project=desktop
PM_LIVE_API_PORT=8832 PM_LIVE_WEB_PORT=3132 \
  npx playwright test --config playwright.live.config.ts --project=phone
```

### One thing the live suite caught that the sample suite could not

"Teach it once" moving into its own collapsed fold (direction 4) means the live journey's own
`getByRole('heading', { name: 'Teach it once. Keep control.' })` check now needs the fold opened
first (`page.locator('.teach-fold summary').click()`) — the sample suite's equivalent path already
opens it for its own reasons and never surfaced this. Fixed in `e2e-live/journey.spec.ts`; the panel
itself is unchanged.

### `web/design-shots/`

Untracked, per `.gitignore` (carried over from r2's `git rm -r --cached`); the PNGs this round's
`design-shots.spec.ts` run produced are on disk, not committed.
