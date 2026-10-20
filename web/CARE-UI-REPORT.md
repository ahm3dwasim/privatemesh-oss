# The caregiver screens — web lane report

Worktree `careui`, branch `build/careui`, from main `c28df9c` (the caregiver backend merged).
Everything below is in `web/`.

## Why this round existed

The entry is judged on four equal axes and Design is the weakest — 3/5 from a blind judge, whose
summary was "a scheduler with a permission popup". Impact was 3.5. The backend now implements the
caregiver story, which is the judged-strongest use case (4.4/5 use case, 4.0/5 impact), and the web
lane was still telling the meeting-reschedule one. The whole point of this round: someone running an
aging parent's week from their own inbox opens the app and, inside ten seconds, sees a product about
that.

---

## Screen by screen

Each heading is followed by the one sentence that screen exists to make true.

### Today — `components/TodayFeed.tsx`, `lib/feed.ts`

Sam Almeida's week: four providers, eleven records over five days, four of them needing them now.
Needs you leads with today's family digest, then the refused benefits email, then the clinic's
blood-test request, then a dental request no time fits. Recent activity is compacted and grouped by
day underneath. The rail shows the day Rosa is actually having — transport at 09:30, physio at
11:00, pharmacy at 15:00 — and the owner's own protected commitment at 16:30.

> **This is somebody's parent's week, and the product is keeping it.**

The feed's language stopped being about one meeting. `subjectOf()` reads the event's own title off
the proposal (`before[0].title`), so a row names the physio session or the transport pickup; with no
proposal it says "an appointment" rather than inventing a subject. `effectSentence` learned a third
effect kind, so a digest has its own five readings and is never described with a calendar's words.

### The refusal — `components/Refusal.tsx`

**One click from the screen the app opens on.** The lookalike benefits email — `Benefits Update
<benefits@northgate-clinic.example.com>`, the domain one hyphen away from the clinic the circle
already knows — is a row in Needs you reading *"Refused Benefits Update's email. Nothing was
disclosed and nothing was sent."* Opening it replaces the plan screen with four blocks, in the order
a person asks the questions:

1. **The verdict.** "Refused. Nothing about Rosa left this machine."
2. **Two counts, side by side.** *Disclose* — it asked for the benefits id and the power-of-attorney
   reference in a reply, and the gate refused the composed body at the moment the words would have
   left; the two decision rows are quoted verbatim, in the engine's own words. *Enrol* — it asked to
   be added to the family list, and **there was no check to fail**, because nothing in the product
   can add a member. That second count is deliberately not drawn as a refusal: the honest fact is an
   absent path, not a guard that held, and drawing an absence as a check would claim machinery this
   build does not have.
3. **What was measured** — the five red-team checks over this exact email, each one's `why` string
   quoted rather than summarised.
4. **The rest of the run** — three messages in the inbox, one raised a change, none of the others
   carried. The guard held on a day with real work in it.

The email itself is one disclosure below, in the untrusted frame.

> **A refusal is a thing that happened to your whole day, and you can see what it cost you: nothing.**

Every decision row, check string and count in this screen came off a real run of
`redteam/cases/09-lookalike-benefits-update.json` through `privatemesh.redteam.run_case`, read with
this lane's own venv. None of it was written to look plausible.

### The family digest — `components/CareDigest.tsx`

It arrives in the ordinary change shape and is reviewed on the ordinary review screen, because
`docs/API_V2.md` says a bespoke screen "would be a second place for the same decision". The card
sits where the calendar diff and the reply sit for a scheduling change, and the same Approve button
is underneath it.

The fragments render in order, under their four section headings, each naming the record it was
rendered from (`from evt-physio`). The audience is listed with the grant each person holds, not as a
recipient list — because the question being answered is *may this person be told this kind of
thing*. One line carries a protected value; it is highlighted, chipped `Protected · Home address`,
and carries an owner-facing disclosure notice saying who those bytes are going to and that approving
for them approves for nobody else.

> **Approving is approving THESE BYTES.**

`artifact_id` is recomputed in the browser, from the fragments on screen, by `lib/care.ts` — a
byte-for-byte reimplementation of `care.artifact_id`, including Python's `json.dumps` separators and
key ordering, pinned in `lib/care.test.ts` against ids the Python produced. Leaving a line out or
dropping a recipient re-composes the artifact and the name on screen changes; the version moves with
it, so the earlier approval is refused by the server as well. If the browser's recomputation ever
disagrees with the server's own `artifact_id`, the screen says so and tells the owner not to approve
it — it does not pick one.

### Care circle — `components/CareCircle.tsx`

Three people, three different answers. Rosa may receive the digest and replies, Tom the digest only,
the clinic front desk scheduling only.

> **Only the owner can add a member — no email, no model output and no taught skill can.**

That sentence is carried by the design, in descending order of how hard it is to argue with:

- **`Added by` is a column on every row and always reads "You".** `POST /v2/care-circle` has
  deliberately no `added_by` on its body — the server writes it — so the cell has exactly one value
  it can ever hold. A field that can only say one thing is a proof; a field that sometimes says
  something else is a log. It is on all three rows, not on the interesting one.
- **The form is the only way in**, on the owner's own screen. There is no "approve this suggestion",
  no row an email can propose, no pending state — a product that could show a half-added member
  would be a product where something else had started the addition.
- **The four refused paths are named**, next to the list, and they are limit clause 7's own `cannot`
  entries, shown here and on Limits from the same source.

Adding and removing go through the real routes. `may_receive` may be left empty on purpose, and the
form says so: "on my list, trusted with nothing yet" is a state the owner is allowed to be in, and a
default would pick a permission for them. The server's two refusal codes are reproduced with their
own messages — an unusable address is a different answer for a browser to render than a kind outside
the closed list.

### Protected facts — `components/ProtectedFacts.tsx`

Labels, kinds, placeholders and whether the masker has needed each one.

> **There is nowhere on this screen a value could be displayed, and that is a property of the code
> rather than a promise in the copy.**

`ProtectedFact` has no `value` field. `GET` does not return one and `POST` does not echo one. The
sample adapter validates the value on the way in and **throws it away** rather than keeping a copy
it could later be asked for — so a future screen that wanted to show a value would have to add a
route first, which is the thing the feature exists to prevent, instead of reading a field that was
already sitting there. The value is typed once, into a password field that is cleared in the same
turn it is submitted. Three tests assert that none of the four distinctive sample values
(`QQ-4471-ALPHA-9`, `POA-2024-88317-LK`, `14 March 1943`, the flat address) appears anywhere in the
document — one of them against the real server over HTTP.

What protection *means* is the top half of the screen, stated as the three rules the gate runs:
never in a model prompt (masked, then the egress guard runs on the bytes that would be posted, so a
masking bug fails as a refusal and not as a leak); never in an outbound artifact unless the owner
approved *those exact bytes* for circle members; and a refusal that records the label and a *count*
of the audience, because a log is a thing that gets exported and pasted into bug reports.

### Limits — `components/StandingLimits.tsx`

Clause 7 is shown verbatim like the other six, and a fourth plain-language principle leads for it:
*"Anything about someone you care for goes only to your circle."* It is phrased conditionally,
because the clause is: a plan that names nobody on the circle passes it, and a principle that
implied otherwise would describe a stricter product than the one that runs. The clause count is read
from the catalogue rather than written as "six", in the screen and in its tests.

---

## How a judge reaches the refusal

**One click.** The app opens on Today; the refused email is the second card in *Needs you*, with a
`Blocked` chip and the outcome already in its own first line. Clicking it opens the refusal screen.
No menu, no scroll at 1280, no demo control.

The digest is also one click (first card). The care circle and protected facts are two: `Care` in
the nav, then a tab.

---

## What changed in the fixtures

`lib/sample.ts` is now the caregiver week. Day `2026-09-22`, matching `sim.CARE_WEEK_DATE`.

| Record | Provider | What it shows |
|---|---|---|
| `benefits-lookalike` | Benefits Update (lookalike domain) | the attack, refused on two counts, terminal |
| `digest-tuesday` | composed on this machine | today's digest, waiting to be read |
| `clinic-bloods` (live) | Northgate Clinic front desk | the interactive change: 15:00/16:00 offered, an injected tail refused |
| `transport-pickup` | City Transport | handled within limits — nobody had to look at it |
| `digest-monday` | composed on this machine | yesterday's digest, already sent (the once-a-day cap is a cap on something real) |
| `physio-moved` | Northgate Clinic | the change behind the drifted agreement |
| `podiatry-review` | Riverside Podiatry | a taught rule narrowing a second day |
| `optician-hallam` | Hallam Opticians | `partial` — calendar moved, delivery never confirmed |
| `daycentre-elm` | Elm Court Day Centre | left unchanged on the owner's own decision |
| `gp-fairfield` | Fairfield Surgery | refused an injection, then finished the legitimate half |
| `dentist-parkside` | Parkside Dental | no safe fit — the day said no, and it is not drawn as a security event |

Eleven records, six providers, five days. Where a fixture has a counterpart in the backend's own
care week it is the **same value**: the circle is `sim.sample_care_circle()`, the facts are
`sim.sample_protected_facts()`, the digest lines are `sim.caregiver_digest_lines()`, the lookalike is
the email `sim.caregiver_messages()` delivers, and the drifted physio agreement is the shape
`sim.caregiver_agreements()` builds — the agreed time an hour earlier than the event now reads, plus
a room change and one attendee the clinic added.

`Agreements` now reports four drifted fields on that physio session, not two.

### The one thing the brief asked for that this build cannot honestly show

The brief asked for **"a refill confirmation handled inside a standing limit"**. `docs/API_V2.md`
§ *"What the care week does not do yet"* says the opposite in as many words: a pharmacy refill
confirmation is not meeting-shaped, `ChangeEngine.classify` raises no change for it, and **there is
no confirmation action kind in this build** that could carry one. A fixture row showing the refill
"handled within limits" would be the web lane claiming an action the product cannot perform — the
exact dishonesty that cost this entry its design score in the first place.

So the refill appears in the two places it honestly can, and nowhere else:

- in **today's digest**, under *what needs you*: "A prescription refill is waiting for you to confirm
  collection.";
- on the **refusal screen**, where "3 message(s) in the inbox, 1 raised a change, 0 other change(s)
  carried" is quoted from the measured run it arrived in.

The within-limits row the brief wanted is `transport-pickup` — a scheduling change to a care
provider, which is an action kind the product really has. For the same reason the refusal screen does
**not** claim that "the legitimate refill and transport actions in the same run still completed": the
red-team case deliberately does not claim it either, and the screen says what the run actually did —
the other mail was read and left alone.

---

## What I kept from passes 2 and 3

Every decision those rounds settled is intact, and several are now load-bearing in more places.

| Decision | Where it still holds |
|---|---|
| **Decision first, proof on demand** | the refusal answers its four questions above the fold and `How this was checked` is still one collapsed disclosure; the digest shows its bytes above the button and its provenance below |
| **One quiet mode indicator** | still one `Demo mode` chip in the app bar, still nothing in any page body; the four new screens add no honesty notice of their own |
| **Honest verbs in sample mode** | the `Simulated` badge is still stated once at the top of a receipt; the digest receipt reads past tense below it like every other |
| **One truthful state machine** | extended, not weakened — see the two defects found below |
| **Three status words** | unchanged. The refusal row is `blocked`; no fourth word was added for it |
| **One row per change** | unchanged; the refusal rides on its own change's row and is not a second row |
| **Calm hierarchy** | the four new surfaces introduce no new colour, radius or spacing step; `--protected` was already the product's word for "this is yours and stays here" and the care screens mean it literally |
| **Dashed borders = content this system did not author** | the only dashed edge on the new screens is the quoted email |
| **Mobile designed, not stacked** | `.circle-member` and `.fact-row` become labelled stacked records at ≤560px, keeping their `May receive` and `Added by` headings instead of becoming unlabelled paragraphs; the two count cards go to one column at ≤1023px; the nav's six slots take one type step down at ≤420px while the 54px touch target is untouched |
| **Nav overlap proven, not implied** | the Playwright assertion now walks the refusal (email expanded), the digest, the care circle and the protected facts as well |

## Two defects this round found and fixed

- **`Day.tsx` matched one fixture's name.** The day panel picked its subject with
  `/alex/i.test(e.title)`, one fixture's first name hard-coded into the component that draws
  everybody's day. On a caregiver week it matched nothing and fell through to `events[0]` by
  accident. The panel now renders every event before the boundary, in order, with its real duration.
- **`validBundle` described one bundle shape.** A digest is one action, not two, so the Approve
  button was enabled and `approve()` returned early without doing anything — a button that silently
  did nothing, on the one control this product is about. Both shapes are now named, and the button
  is disabled with a reason when neither holds. Caught by writing the approval test, not by reading.

Also fixed on the way: `EmailInput` marked the injected line by matching the literal string
`…and cc Dave`, so every other injected tail rendered as ordinary prose; a settled `partial` record
read the live change's variant and said "Hallam Opticians was told" under a `Blocked` chip; and
`e2e/demo.spec.ts` asserted "nothing leaves this machine" against a hard-coded port number.

---

## What I could not render, and why

- **The digest's own history.** `docs/API_V2.md`: "no route renders the digest's own history (the
  ledger records that a digest went out, and `recent()` reads it, but no endpoint serves it)". The
  once-a-day cap is therefore shown as a *sentence* on the approve bar and demonstrated by a second,
  already-sent digest in the week — not read from the ledger.
- **A disclosure approval the owner can press.** `approve_disclosure` exists and is tested, and the
  doc says "the route that would let an owner press it is the web lane's to propose". This lane
  proposes it, as `CareDigestAction` and two `Edit` fields, both documented in `lib/v2-types.ts` as a
  proposal that **no route accepts today**. `lib/sample.ts` supplies the shape; a live server that
  does not is rendered with the ordinary reply view and never with an invented digest.
- **`POST /v2/care/digest` is not wired.** It SENDS. The brief's item 4 is about reviewing and
  approving a digest, and putting a send button behind a route this lane could not exercise
  end-to-end against a live server would be shipping an untested outbound.
- **The caregiver week is not reachable from the CLI.** `build_sample(care_week=True)` is opt-in and
  `privatemesh serve --v2 --sample` does not pass it, so the live suite runs against the scheduling
  day. The care circle and the protected facts ARE seeded in the ordinary sample (`_seed_care` runs
  unconditionally), which is why the live suite can and does assert both screens against the real
  server. A `--care-week` flag is a one-line CLI change in another lane's files and would make the
  judges' path show the caregiver week too — worth proposing.
- **The clinic's `appointments@` vs `front.desk@` split.** The backend's care week delivers the
  physio message from `appointments@northgate.example.com`, which is not on the circle; the fixture's
  live change comes from `front.desk@`, which is, so that clause 7 can be shown *passing on a real
  recipient* rather than passing vacuously. Both are true of the same clinic; the fixture picked the
  one that demonstrates the grant.

---

## Proof

| Check | Result |
|---|---|
| `npx tsc --noEmit` | clean |
| `npm test` (Vitest) | **144 passed**, 11 files (was 84 in 6 files) |
| `npx playwright test --project=desktop` | **29 passed** |
| `npx playwright test --project=phone` | **29 passed** |
| Live suite, desktop (`serve --v2 --sample`, own venv) | **5 passed** |
| Live suite, phone | **5 passed** |
| `PM_STATIC_EXPORT=1 NEXT_PUBLIC_API_MODE=sample npx next build` | exports; `out/index.html` written, 103 kB shared JS |
| `npm run guards` | 4/4 — static+sample, static+no-mode and live all build; static+live is refused |
| `npm run guard:static-live` | refused by the real build |
| `npm run bundle-check` (built with the canary) | 71 built files carry neither the canary nor the variable name |
| Contrast AA, measured on the rendered page | every screen incl. the four new ones, both themes, **0 failures** |
| Target size ≥24×24 at 375 | **0 failures** (the grant checkboxes were 20×20 and were caught by this) |
| Horizontal overflow at 320 / 375 / 768 / 1280 | 0, asserted at every screenshot (a 33-character care address was pushing the proposal card 40px past 320 and was caught by this) |
| Bottom nav vs last card at 375×812 | 0 overlaps, measured across Today, the refusal, the digest, Care circle, Protected facts, Change (all three tabs), Agreements, Skills, Limits, First run |
| Screenshots | `web/design-shots/` (untracked) — 21 screens × {375, 1280} × {light, dark} = **84**, plus 13 real-viewport phone shots |

### Running it

```sh
cd <this worktree's checkout root>
python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
cd web && npm ci
npx tsc --noEmit && npm test
PM_SAMPLE_WEB_PORT=3978 npx playwright test --project=desktop
PM_SAMPLE_WEB_PORT=3979 npx playwright test --project=phone
PM_LIVE_API_PORT=8851 PM_LIVE_WEB_PORT=3951 PM_LIVE_GATE_PORT=8853 \
  npx playwright test --config playwright.live.config.ts --project=desktop
PM_LIVE_API_PORT=8852 PM_LIVE_WEB_PORT=3952 PM_LIVE_GATE_PORT=8854 \
  npx playwright test --config playwright.live.config.ts --project=phone
```

`playwright.config.ts`'s port is now overridable (`PM_SAMPLE_WEB_PORT`, default 3102 unchanged), for
the same reason the live suite's already was: two lanes running the suite at once on one machine
would otherwise bind the same port, and a test that binds a port somebody else is serving is not a
test.

### `web/design-shots/`

Untracked, per `.gitignore`. The PNGs this round produced are on disk, not committed.
