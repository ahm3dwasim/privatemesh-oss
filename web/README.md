# PrivateMesh web v2

An app with six places, in light and dark:

| Place | What it answers |
| --- | --- |
| **Today** | What did the assistant handle, what is waiting on me, and what did it refuse? |
| **Care** | Who may be told about the person I care for, and what may each of them be told? |
| **Agreements** | Are the meetings it set up still what I agreed to? |
| **Skills** | What have I taught it, and what did that rule remove today? |
| **Limits** | How much may it do on its own, and in exactly which words? |
| **Settings** | What is connected, and what has it been told about me? |

A change opens from a row in the Today feed on one canonical summary — what was asked, what the
bundle does, what was blocked — with the decision controls right under it, sticky on a phone. The
trust boundary this product is built on is one click away, in "How this was checked": **what
arrived** (the untrusted email) → **what the model proposed** (the suggestion, plus the verbatim
model receipt) → **what trusted code allowed** (the exact diff, the exact reply, the clause verdicts,
and the decision log). The first two columns are recessed and dashed; the third is raised. That one
rule is the visual grammar of the whole product. On a phone the three stack in that order, and
technical words — untrusted, trusted code, validator, allowlisted — live only inside that section.

A first-run path — connect or use the sample, pick a limit preset, teach a rule — replaces Today
only while there is no history at all.

Next.js App Router, strict TypeScript, Vitest, Playwright. Design tokens, the type scale and both
themes live in `app/globals.css`.

## Run the isolated sample

```sh
cd web
npm ci
npm run fonts
npm run dev
```

`NEXT_PUBLIC_API_MODE=sample` is the default. No keys are required. All venue facts, identities,
connections and effects are synthetic. The sample adapter never fetches a provider. The sample date
is 19 September 2026, Europe/London, and scenario switching resets its in-memory state.

`npm run fonts` downloads Source Sans 3 and its SIL license from the Google Fonts repository to
`public/fonts`. Manrope and its SIL license are already bundled. Fonts are served locally at runtime;
there is no Google Fonts runtime request. Source Sans 3 was not downloadable in the restricted build
session: until the command succeeds, body text uses the explicitly configured system fallback.

## Try it live — the public demo (`NEXT_PUBLIC_API_MODE=demo`)

Build with `NEXT_PUBLIC_API_MODE=demo` and the root page (`/`) becomes `components/DemoApp.tsx`
instead of the sample workspace: a visitor starts a session against `POST /v2/demo/session` (a real
model, simulated providers, hard caps — see the backend's `docs/API_V2.md` § "The public demo"),
reads the seeded inbox, and either runs the built-in lookalike "benefits update" attack or writes
their own email. Two buttons run the SAME email through the SAME engine, boundary and gate two
different ways, and both results render **side by side**: "Send to the real model"
(`POST /v2/demo/session/{id}/email`) spends a real model call against the session's own budget, and
"Run the compromised model (free)" (`POST /v2/demo/session/{id}/email/compromised`) runs the
red-team corpus's own fully-compliant scripted model instead — labelled honestly as "Scripted, not
Nemotron" everywhere it appears, zero spend, no model call, still subject to the demo's rate limit.
Each response — the model's actual receipt, the decision log, the proposal or refusal, and the
remaining budget — is rendered from what that route actually returned; nothing about the real run is
scripted except the fallback the backend itself degrades to once a cap is reached, and nothing about
the compromised run pretends to be the real model.

The inbox is the **caregiver week**, not the scheduling sample: the same four messages
`redteam/cases/09-lookalike-benefits-update.json` is measured against, with the pharmacy refill, the
moved physio appointment, the transport booking and the lookalike benefits email in it. "Run the
built-in attack" therefore SELECTS that benefits email and loads the corpus's `injection` field into
it — all four sentences, including the scheduling line that gives the engine a time to propose, so
the run reaches the privacy boundary instead of stopping before a draft exists. Against the real
model this typically produces a proposal that never touched the injection; against the compromised
model it produces a "What it tried" list (redacted of every protected value) followed by a refusal
that names the protected fact's **category** (`The reply (Benefits id)`) and never any part of a
value — the contrast between the two columns is the point. `lib/demo-fixtures.ts` carries the exact
strings and `components/DemoApp.test.tsx` asserts that no protected value reaches the page from
either run. Calendar times render human-readably ("Tue 22 Sep, 13:00 → 13:45"), never as raw ISO.

The scripted sample workspace (no model) is never removed — it moves to `/tour` and stays
linked from the top of the demo landing as "Sample workspace (no model)". Every other build
(`NEXT_PUBLIC_API_MODE` unset, `sample`, or `live`) is unchanged: the root page is still the sample
workspace (or the live client) exactly as before, and `/tour` also renders it.

`lib/demo-api.ts` is the transport for the four demo routes; it reuses `NEXT_PUBLIC_API_BASE_URL`
(the same variable the live client reads) rather than a separate one, since a build only ever
points at one backend. These routes are public by design — no owner token, no loopback
restriction — because a demo a stranger cannot reach without a credential is not a demo.

Every cap the backend enforces is rendered, not just the happy path: a session limit or a missing
model key is `503 demo_unavailable`, a caller going too fast is `429 rate_limited`, an empty or
oversized email is `422` before a model call is spent, and an unknown or expired session is `404`
with a "start a new session" recovery — none of these ever produce a blank screen.

Screenshots of every state, at 1440×900 and 375×812, are `npm run e2e:demo`'s job — see below.

## Live integration

Build with `NEXT_PUBLIC_API_MODE=live`. `web/lib/api.ts` is the only UI transport. Requests use `/v2`
on the same origin, or `NEXT_PUBLIC_API_BASE_URL` if explicitly configured. Authentication is the
backend's responsibility; approval is disabled unless the returned session explicitly identifies an
authenticated owner. Configure a same-origin reverse proxy for credentialed live deployments.
The public proposal illustration remains synthetic even in live mode.

See [API_V2.md](../docs/API_V2.md) for additive fields/routes agreed by this frontend. They include
boundary setup, connection status, independent calendar attendees, preference editing and provenance.
The parallel engine implementation must supply those fields before live acceptance. No provider,
account authorization, real send, or calendar mutation was exercised by this build.

## Checks and screenshots

```sh
npm run build
npm test
npm run e2e
npm run e2e:demo
```

`npm run e2e:demo` (`playwright.demo.config.ts`, `e2e-demo/demo.spec.ts`) builds with
`NEXT_PUBLIC_API_MODE=demo` and proves the "Try it live" landing against a MOCKED `/v2/demo/*` —
route interception, never a real model and no key needed — covering session start, the built-in
attack's refusal against the real model, the two runs side by side (real model proposes without
attempting the injection; compromised model tries everything and is refused), a custom email's
proposal, and a budget-exhausted run's honest degradation, at 1440×900 and at 375×812
(`design-shots/demo/`, tracked). Run `npx playwright install chromium` first if Chromium is not
already on this machine. Its port defaults to 3151 (`PM_DEMO_WEB_PORT` to change it) and is never
8765, 8791, 3000, or the other suites' ports.

Playwright builds and starts the production sample on port 3102. It covers 1440×900 and 390×844,
hero/stale/partial/unknown flows, setup/empty/no-fit/cancel/edit/expiry/offline/long-wait states,
keyboard navigation, 320×568/768/1280 reflow, and a 720×450 CSS viewport equivalent to 200% zoom
on a 1440×900 display. This last case is a reflow test, not proof of native browser zoom controls.
Screenshots are written to gitignored `.shots/`. `npm run verify` runs all three checks. Browser
binaries must be available to Playwright.

Two further specs are measurements rather than journeys:

* `e2e/contrast.spec.ts` walks every rendered screen in both themes, resolves the real backdrop
  behind every word and every control edge, and fails below WCAG AA (4.5:1 text, 3:1 large text and
  component boundaries). It also asserts the 24×24 target-size floor at 375. It freezes transitions
  before reading — a control that has just become enabled still reports the disabled colour
  otherwise, which measures a state the page never rests in.
* `e2e/design-shots.spec.ts` writes every screen to `web/design-shots/` at 375 and 1280 in both
  themes, and asserts only that the page never scrolls sideways at the width it was captured at.

The original restricted build session could run compilation and Vitest but could not bind a server
or launch Chromium. No screenshot files or visual acceptance are claimed from that session.

## The live path (`npm run e2e:live`)

`npm run e2e` proves the client against its OWN fixture. `npm run e2e:live`
(`playwright.live.config.ts`) proves it against a real backend: it starts
`privatemesh serve --v2 --sample` on port 8831 and a `NEXT_PUBLIC_API_MODE=live` build of this app on
3131, and drives Today → learn → permission → install → prepare → review → approve → outcome →
`/why` → a second change served from the library → a stale refusal, at 1440×900 and 390×844.

The two suites cannot run at the same time: both write `web/.next`, and the live one needs the ports
above to be free. The backend holds the sample's state in memory, so the phone run gets a second,
fresh backend on 8832/3132 — `e2e:live` chains the two runs itself. Screenshots go to gitignored
`.shots-live/`.

Neither port is 8765. That port belongs to the owner's own server; a test that binds it, or kills a
process it did not start, is an outage rather than a test.

The live backend's `mode` decides what this UI calls itself: `api.session()` is read, and a
synthetic workspace is labelled as one even though the client was built live. See
`sampleControls`/`sampleWorkspace` in `app/page.tsx`.

## Useful sample entry points

- `/?variant=happy`
- `/?variant=stale`
- `/?variant=partial`
- `/?variant=unknown`
- `/?variant=expired`
- `/?variant=no_safe_fit`
- `/?variant=empty` — the first-run path
- `/?variant=limit_handoff` — a clause hands the change back

The in-app “Explore sample situations” control also exposes disconnected, authorizing, expired
connection, paused routine, empty and long-wait cases. Sample approval binds the current version,
expiry and boundary. Editing revalidates, changes the version, and requires a new review. Unknown
provider outcomes never trigger an automatic resend.
