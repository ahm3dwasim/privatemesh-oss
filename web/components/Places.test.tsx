import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import Home from '@/app/page';
import { resetSample, sampleLimitClauses, SAMPLE_CHANGE_ID } from '@/lib/sample';
import { api } from '@/lib/api';
import { buildFeed, effectSentence, refusalPhrase, withheldPhrase } from '@/lib/feed';
import type { Change, Decision, Today } from '@/lib/v2-types';

async function renderApp(variant = 'happy') {
  window.history.replaceState({}, '', `/?variant=${variant}`);
  await act(async () => { render(<Home />); });
}
async function click(name: string) { await act(async () => { fireEvent.click(screen.getAllByRole('button', { name })[0]!); }); }
async function clickSelector(selector: string) {
  const node = document.querySelector(selector);
  if (!node) throw new Error(`No element matched ${selector}`);
  await act(async () => { fireEvent.click(node); });
}
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal('scrollTo', vi.fn()); resetSample(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

/* ---------------- Today feed ---------------- */

test('the feed opens with one plain sentence per event, and a refusal rides on its own row', async () => {
  await renderApp();
  const rows = [...document.querySelectorAll('.feed-row')];
  expect(rows.length).toBeGreaterThanOrEqual(2);
  // The live change's own row, found by its id: the caregiver week has several things needing the
  // owner, and "the first row" names none of them.
  const live = document.querySelector(`[data-change="${SAMPLE_CHANGE_ID}"]`)!;
  expect(live).toHaveTextContent('Northgate Clinic asked to move an appointment.');
  expect(live).toHaveTextContent('Needs you');
  // What this thread refused rides ON its own card, not as a second row for the same meeting.
  expect(live).toHaveTextContent('Also blocked');
  expect(live).toHaveTextContent('Refused: the email tried to add a recipient and share a private detail.');
  expect(screen.getByText(/Watching your inbox/)).toBeInTheDocument();
  // The believable week seeds a `no_safe_fit` change alongside today's own `detected` change, so two
  // things need the owner. The seeded `partial` change is a finished, blocked result — it renders
  // under Recent activity, not Needs you — so it must not inflate this count either.
  expect(screen.getByText(/changes need you/)).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Needs you', level: 2 })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Recent activity', level: 2 })).toBeInTheDocument();
});

test('the Today permission control is a labeled setting, not explanatory copy', async () => {
  await renderApp();
  const control = document.querySelector('.standing-card')!;
  expect(within(control as HTMLElement).getByText('Autonomy')).toBeInTheDocument();
  expect(within(control as HTMLElement).getByText('Ask before acting')).toBeInTheDocument();
  expect(within(control as HTMLElement).getByRole('button', { name: 'Manage limits' })).toBeInTheDocument();
});

test('the "Needs you" counter always equals the number of rows rendered under it', async () => {
  await renderApp();
  const needsYouSection = screen.getByRole('heading', { name: 'Needs you', level: 2 }).closest('section')!;
  const rendered = needsYouSection.querySelectorAll('.feed-row').length;
  const stated = Number(screen.getByText(/changes? needs? you/).textContent?.match(/(\d+)/)?.[1]);

  // The expected number comes from the SERVER's own answer, not from the page. Comparing the
  // counter with the rows it is rendered above is the sixth review's test-quality item: both sides
  // are produced by `buildFeed` from the same input, so a wrong predicate in `buildFeed` makes them
  // wrong TOGETHER and the assertion passes on a screen showing the wrong number of the wrong rows.
  // `plans()` is a second implementation of "what needs the owner" — it reads the change's STAGE and
  // returns `state` — so a drift between the two is what this now catches.
  const seeded = await api.plans();
  const expected = seeded.filter(plan => plan.state === 'needs_you').length;
  expect(seeded.length).toBeGreaterThan(expected);   // settled history is present, so the count is a distinction
  expect(expected).toBeGreaterThan(0);
  expect(stated).toBe(expected);
  expect(rendered).toBe(expected);
});

test('a completed run reports the authority that ran it', async () => {
  await renderApp();
  await clickSelector(`[data-change="${SAMPLE_CHANGE_ID}"] .feed-hit`);
  await click('Handle this change');
  await advance(2600);
  await click('Approve this change');
  await advance(2400);
  await click('Today');
  // Found by the change's own id, not by position: "the first compact row" is whichever of the
  // week's settled appointments happens to sort first, and that is not what this test is about.
  const recent = document.querySelector(`.feed-day [data-change="${SAMPLE_CHANGE_ID}"]`)!;
  expect(recent).toHaveTextContent('Done');
  expect(recent).toHaveTextContent('Moved Blood test — Rosa to 15:00 at Northgate Clinic, Room 4, and replied to Northgate Clinic.');
});

test('two change records describing the same event dedupe to the newer one, by stable identity', () => {
  const today = { date: '2026-09-19', timezone: 'Europe/London', events: [], protected: [], changes: [] } as Today;
  const proposal = (start: string) => ({
    start, end: start, venue: { name: 'v', address: 'a', note: '', evidence: { url: null, title: '', excerpt: '', retrieved_at: '', official: false, sample: true } }, venues: [], rejected: [],
    reason_sentence: '', before: [], after: [], protected: { id: 'p', start, end: start, buffer_minutes: 0, label: 'Protected' as const }, privacy_line: '', expires_at: start,
    actions: [{ kind: 'calendar_update' as const, event_id: 'same-event', attendees: [], diff: { start: [start, start] as [string, string], end: [start, start] as [string, string], location: ['x', 'Café X'] as [string, string] }, notifies_attendees: true }],
  });
  const older: Change = { id: 'a', version: 1, stage: 'done', request_excerpt: '', proposal: proposal('2026-09-17T15:00:00+01:00'), blocked_reason: null, blocked_attempts: 0, updated_at: '2026-09-17T16:00:00+01:00', executed_by: 'owner' };
  const newer: Change = { id: 'b', version: 1, stage: 'done', request_excerpt: '', proposal: proposal('2026-09-17T15:00:00+01:00'), blocked_reason: null, blocked_attempts: 0, updated_at: '2026-09-19T20:00:00+01:00', executed_by: 'owner' };
  const items = buildFeed(today, [older, newer], {});
  expect(items).toHaveLength(1);
  expect(items[0]!.change_id).toBe('b');
});

const emptyDay = { date: '2026-09-19', timezone: 'Europe/London', events: [], protected: [], changes: [] } as Today;
const row = (verdict: Decision['verdict'], subject = 'anything'): Decision =>
  ({ seq: 1, at: '2026-09-19T10:00:00+01:00', stage: 'validate', source: 'system', untrusted: false, subject, verdict, rule: 'r', detail: null });
const changeWith = (over: Partial<Change> = {}): Change =>
  ({ id: 'x', version: 1, stage: 'done', request_excerpt: '', proposal: null, blocked_reason: null, blocked_attempts: 0, updated_at: '2026-09-19T10:00:00+01:00', executed_by: 'owner', ...over }) as Change;

test('the feed never guesses a refusal it could not read', () => {
  // The decision log is unavailable, but the change itself reports how many requests were stopped.
  // The row must say the number and nothing about which one.
  const change = changeWith({ stage: 'detected', blocked_attempts: 2 });
  const item = buildFeed(emptyDay, [change], {})[0]!;
  expect(item.blocked?.kind).toBe('stopped');
  expect(item.blocked?.line).toBe('Stopped 2 requests on this thread.');
  expect(item.blocked?.detail).toMatch(/count, not the reasons/);
  // `blocked_attempts` is refused PLUS withheld, so the row cannot attribute a reason: "went beyond
  // what you allow" was true of at most some of the two, and this test is what stops it coming back.
  expect(item.blocked?.line).not.toMatch(/you allow|beyond|Refused/);
});

test('a withheld decision is never reported as a refusal', () => {
  // Both halves, because the bug was a surface that answered for both verdicts: the withheld phrase
  // must say the request went ahead, and a withheld row through the feed must not pick up a
  // refusal's words. A test of only one side passes while the other regresses.
  expect(withheldPhrase()).toMatch(/went ahead without everything it asked for/);
  expect(withheldPhrase()).not.toMatch(/Refused|Nothing was sent/);

  const withheld = buildFeed(emptyDay, [changeWith({ blocked_attempts: 1 })], { x: [row('withheld')] })[0]!;
  expect(withheld.blocked?.kind).toBe('withheld');
  expect(withheld.blocked?.line).toBe(withheldPhrase());
  expect(withheld.blocked?.line).not.toMatch(/Refused/);
  expect(withheld.blocked?.detail).not.toMatch(/Nothing was sent/);
  expect(withheld.blocked?.detail).toMatch(/Nothing was refused/);

  // And the refused side is unchanged, so the separation is a distinction and not a hole: a real
  // refusal still reads as one.
  const refused = buildFeed(emptyDay, [changeWith({ blocked_attempts: 1 })], { x: [row('refused', 'Share a private detail with an extra recipient')] })[0]!;
  expect(refused.blocked?.kind).toBe('refused');
  expect(refused.blocked?.line).toBe(refusalPhrase(row('refused', 'Share a private detail with an extra recipient')));
  expect(refused.blocked?.line).toMatch(/^Refused:/);
  expect(refused.blocked?.detail).toBe('Nothing was sent by the refused request.');
});

test('a discarded candidate is neither a refusal nor counted as one', () => {
  // The fourth verdict, at the surface the owner reads. A sender who offers 15:00 and 16:00 gets one
  // slot taken and one left; the row for that change must read as an ordinary choice.
  const rows = [row('allowed', 'Accepted 15:00–15:45'), row('discarded', 'Not chosen 16:00–16:45')];
  const item = buildFeed(emptyDay, [changeWith()], { x: rows })[0]!;
  expect(item.blocked).toBeUndefined();
  expect(item.line).not.toMatch(/Refused|blocked/i);
  // And when a real refusal rides alongside it, the refusal is what the note reports.
  const both = buildFeed(emptyDay, [changeWith({ blocked_attempts: 1 })], { x: [...rows, row('refused', 'Refused 17:00–17:45')] })[0]!;
  expect(both.blocked?.kind).toBe('refused');
});

test("a partial change reports each effect's own state, never 'did not send' for an unknown delivery", () => {
  // The finding: `partial` is a stage, not a fact. The sample's own two variants are the proof —
  // one has the reply failed, the other has its delivery unknown — and the row used to print the
  // same sentence for both. Asserted against the OUTCOME fixture, so a mapping that collapses the
  // two states fails here no matter which side of it is wrong.
  const outcome = (calendar: string, reply: string) => ({ summary: '', effects: [
    { kind: 'calendar_update' as const, state: calendar as 'done', detail: '' },
    { kind: 'email_reply' as const, state: reply as 'done', detail: '' },
  ] });
  // Server-shaped: the name comes from the day's own summary row, not from a string in the test.
  const day = { ...emptyDay, changes: [{ id: 'x', from_name: 'Alex', from_address: 'alex@example.com', received_at: '2026-09-19T09:00:00+01:00', excerpt: '', thread_url: null, stage: 'partial' }] } as Today;
  const line = (calendar: string, reply: string) =>
    buildFeed(day, [changeWith({ stage: 'partial' })], {}, { x: outcome(calendar, reply) })[0]!.line;

  const unknownDelivery = line('done', 'unknown');
  expect(unknownDelivery).toMatch(/Whether Alex was told is unknown/);
  expect(unknownDelivery).not.toMatch(/did not send|was not told/);

  const failedSend = line('done', 'failed');
  expect(failedSend).toMatch(/Alex was not told — the reply did not send/);

  const unconfirmedMove = line('unknown', 'done');
  expect(unconfirmedMove).toMatch(/Whether an appointment moved.*is unknown/);
  expect(unconfirmedMove).not.toMatch(/^Moved /);

  // No outcome read yet: the row claims nothing about which effect finished, rather than filling in.
  const unread = buildFeed(day, [changeWith({ stage: 'partial' })], {}, {})[0]!.line;
  expect(unread).toMatch(/has not been read, so this row claims nothing/);
});

test('effectSentence is the one place an effect state becomes words', () => {
  // The unit the two surfaces share. `unknown` is the state that must never be rendered as `failed`.
  const meeting = { who: 'Alex', when: '15:00', where: 'Fern & Finch Café', what: 'your catch-up with Alex' };
  expect(effectSentence({ kind: 'email_reply', state: 'unknown' }, meeting)).toBe("Whether Alex was told is unknown — the reply's delivery was not confirmed.");
  expect(effectSentence({ kind: 'email_reply', state: 'failed' }, meeting)).toBe('Alex was not told — the reply did not send.');
  expect(effectSentence({ kind: 'calendar_update', state: 'unknown' }, meeting)).toMatch(/Whether your catch-up with Alex moved/);
  expect(effectSentence({ kind: 'calendar_update', state: 'done' }, meeting)).toBe('Moved your catch-up with Alex to 15:00 at Fern & Finch Café.');
  // `running` is not `pending`. A send whose request is already in flight may have reached the
  // provider, so it must not be described with the words a send nothing attempted gets — the same
  // rule as `unknown`, in the same direction, one state earlier.
  expect(effectSentence({ kind: 'email_reply', state: 'pending' }, meeting)).toBe('The reply to Alex has not been sent.');
  expect(effectSentence({ kind: 'email_reply', state: 'running' }, meeting))
    .toBe('The reply to Alex is being sent — it may already have reached them.');
  expect(effectSentence({ kind: 'calendar_update', state: 'running' }, meeting)).toMatch(/may already have gone through/);
  // Every state has words, so a new one cannot silently render as blank — and none of them asserts
  // an outcome nothing observed. `not been sent` is true of `pending` and of nothing else.
  for (const kind of ['calendar_update', 'email_reply'] as const)
    for (const state of ['pending', 'running', 'done', 'failed', 'unknown'] as const) {
      const words = effectSentence({ kind, state }, meeting);
      expect(words).not.toMatch(/undefined/);
      expect(words.trim()).toBe(words);
      if (state !== 'pending')
        expect(words).not.toMatch(/has not been sent|has not finished/);
    }
});

/* ---------------- first run ---------------- */

test('first run replaces Today only when there is no history at all', async () => {
  await renderApp('empty');
  expect(screen.getByRole('heading', { name: 'Set it up once.' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Choose how much it may do on its own' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Teach it one rule' })).toBeInTheDocument();
  await click('Teach a rule');
  expect(screen.getByRole('heading', { name: 'Teach it once. Keep control.', level: 1 })).toBeInTheDocument();
  cleanup();
  // A day with a change is not a first run, however little has happened on it.
  await renderApp('happy');
  expect(screen.queryByRole('heading', { name: 'Set it up once.' })).not.toBeInTheDocument();
});

test('completing every setup step still needs "Start using PrivateMesh" to leave setup', async () => {
  await renderApp('empty');
  // Sample mode's "connect" step is already satisfied (there is nothing to connect for real), so
  // progress starts pointing at step 2, not step 1.
  expect(screen.getByText('Step 2 of 3')).toBeInTheDocument();
  await click('Open limits');
  await click('Act within limits');
  await click('Confirm mode change');
  await click('Today');
  expect(screen.getByText('Step 3 of 3')).toBeInTheDocument();
  await click('Teach a rule');
  await click('Nothing after 15:30');
  await click('Teach this rule');
  await advance(2600);
  await click('Review skill permissions');
  await click('Install this skill');
  await click('Today');
  expect(screen.getByRole('heading', { name: 'Setup complete.' })).toBeInTheDocument();
  await click('Start using PrivateMesh');
  expect(screen.queryByRole('heading', { name: 'Set it up once.' })).not.toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Today', level: 1 })).toBeInTheDocument();
});

/* ---------------- limits ---------------- */

test('the limits screen names the off mode “Watch only” and still sends `off`', async () => {
  await renderApp();
  await click('Limits');
  expect(screen.getByRole('button', { name: 'Watch only' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Off' })).not.toBeInTheDocument();
  const save = vi.spyOn(api, 'setLimits');
  await click('Watch only');
  await click('Confirm mode change');
  // The label is the owner's; the value is the API's. They are allowed to differ and must not drift.
  expect(save).toHaveBeenCalledWith('off');
  expect((await api.limits()).mode).toBe('off');
  expect(screen.getByText(/Standing answer saved: Watch only/)).toBeInTheDocument();
  save.mockRestore();
});

test('every exact clause and its “it cannot” list sits behind one panel, summarised as four principles', async () => {
  await renderApp();
  await click('Limits');
  expect(screen.getByRole('heading', { name: 'The four principles' })).toBeInTheDocument();
  expect(document.querySelectorAll('.principle')).toHaveLength(4);
  // Every clause the catalogue carries, verbatim and exactly once — including clause 7, the care
  // clause, which is what would have been missing if the care work had added a rule the Limits
  // screen could not show.
  for (const clause of sampleLimitClauses) expect(screen.getByText(clause.text)).toBeInTheDocument();
  const panel = document.querySelector('.clauses-exact-panel')!;
  expect(panel).not.toHaveAttribute('open');
  const clauseNodes = [...panel.querySelectorAll('.clause')];
  expect(clauseNodes).toHaveLength(sampleLimitClauses.length);
  for (const [index, node] of clauseNodes.entries())
    for (const item of sampleLimitClauses[index]!.cannot)
      expect(within(node as HTMLElement).getByText(item)).toBeInTheDocument();
});

/* ---------------- skills: the narrowing view ---------------- */

test('a taught rule shows what it removed, struck through, and states that it can only remove', async () => {
  await renderApp();
  await click('Skills');
  expect(screen.getByText('A taught rule can only remove options. It can never add one, move one or send anything.')).toBeInTheDocument();
  await click('Nothing after 15:30');
  await click('Teach this rule');
  await advance(2600);
  await click('Review skill permissions');
  expect(screen.getByText('An offered time at 16:00')).toBeInTheDocument();
  await click('Install this skill');

  // Preparing with the rule installed puts its refusal on the plan, named by the skill, and the
  // card reads the narrowing straight out of that list rather than claiming it.
  await click('Today');
  await clickSelector(`[data-change="${SAMPLE_CHANGE_ID}"] .feed-hit`);
  await click('Handle this change');
  await advance(2600);
  await click('Skills');
  const card = [...document.querySelectorAll('.skill-card')].find(c => c.textContent?.includes('Late-afternoon cutoff'))!;
  expect(card).toHaveTextContent('Removed 1 of the 2 times it was offered on today’s change.');
  expect(card.querySelector('.slot.kept .slot-time')).toHaveTextContent('15:00');
  expect(card.querySelector('.slot.removed s')).toHaveTextContent('16:00 start');
  expect(card).toHaveTextContent('A rule cannot put a time back.');
});

test('an installed rule that has not run yet says so rather than claiming a use', async () => {
  await renderApp();
  await click('Skills');
  await click('Never before 10am');
  await click('Teach this rule');
  await advance(2600);
  await click('Review skill permissions');
  await click('Install this skill');
  // The sample seeds one taught rule already active (with its own real narrowing from three days
  // ago), so the card this test cares about is the one it just installed, not necessarily the first.
  const cards = [...document.querySelectorAll('.skill-card')];
  const card = cards.find(c => c.textContent?.includes('No early mornings'));
  expect(card).toHaveTextContent('No plan has been prepared since you taught this. It has removed nothing.');
});

/* ---------------- agreements ---------------- */

test('agreements list drift field by field and never read an unchecked row as clean', async () => {
  await renderApp();
  await click('Agreements');
  expect(screen.getByText('1 meeting has changed since you agreed.')).toBeInTheDocument();
  const drifted = document.querySelector('.agreement.drifted')!;
  expect(drifted).toHaveTextContent('Changed since you agreed');
  // Rosa's physiotherapy: agreed for 10:00, and the calendar now reads 11:00. Nobody failed and
  // nobody was emailed — the clinic moved it, and this row is the only place the owner finds out.
  const rows = [...drifted.querySelectorAll('.drift-row')];
  // Four fields, one row each: an hour later, a different room, and an address the owner never
  // agreed to. Reported field by field rather than as "this changed".
  expect(rows).toHaveLength(4);
  expect(drifted).toHaveTextContent('Physio — Rosa');
  expect(drifted).toHaveTextContent('1 person you did not agree to');
  expect(drifted).toHaveTextContent('The assistant did not make these changes and will not undo them.');
  // Healthy agreements are collapsed into compact rows behind a summary, not full cards.
  expect(screen.getByText(/meetings are holding as agreed/)).toBeInTheDocument();
});

test('a new agreement starts unchecked, with a reason and a recovery action, and checking resolves it', async () => {
  await renderApp();
  await clickSelector(`[data-change="${SAMPLE_CHANGE_ID}"] .feed-hit`);
  await click('Handle this change');
  await advance(2600);
  await click('Approve this change');
  await advance(2400);
  await click('Agreements');
  // The reason it is unchecked is stated once for the whole group, not repeated on every compact row.
  expect(screen.getByText(/This is not a problem/)).toBeInTheDocument();
  // The believable week seeds its own unchecked and holding agreements alongside this freshly
  // recorded one, so the row this test cares about is found by the meeting it names, not by being
  // the only unchecked row on the page.
  const fresh = [...document.querySelectorAll('.agreement.unchecked')].find(el => el.textContent?.includes('Blood test for Rosa'))!;
  expect(fresh).toHaveTextContent('Not checked yet');
  expect(fresh).toHaveTextContent('Never checked');
  expect(within(fresh as HTMLElement).getByRole('button', { name: 'Check now' })).toBeInTheDocument();
  await click('Check the calendar now');
  expect([...document.querySelectorAll('.agreement.unchecked')].some(el => el.textContent?.includes('Blood test for Rosa'))).toBe(false);
  // Every previously unchecked row (this one, and the week's own) now reads as holding.
  expect(document.querySelectorAll('.agreement.holding').length).toBeGreaterThanOrEqual(2);
  // Looking again does not undo what moved.
  expect(document.querySelectorAll('.agreement.drifted')).toHaveLength(1);
});

test('a server without the agreements route claims nothing', async () => {
  // `agreements` is optional on the contract — the engine lane is implementing the route — so the
  // spy has to name the shape where it is present.
  const missing = vi.spyOn(api as Required<typeof api>, 'agreements').mockRejectedValue(new Error('404'));
  try {
    await renderApp();
    await click('Agreements');
    expect(screen.getByText(/This server does not report agreements yet/)).toBeInTheDocument();
    expect(document.querySelector('.agreement')).toBeNull();
  } finally { missing.mockRestore(); }
});
