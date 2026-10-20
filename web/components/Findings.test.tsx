import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import Home from '@/app/page';
import { ChangeDetail } from './ChangeDetail';
import { FirstRun } from './FirstRun';
import { api } from '@/lib/api';
import { resetSample, samplePreview, SAMPLE_CHANGE_ID } from '@/lib/sample';
import { safeHttpUrl } from '@/lib/urls';
import type { Change, Decision, Outcome, Session, Today } from '@/lib/v2-types';

/**
 * The UI half of the audit, one test per finding, each asserting the PROPERTY the finding is about
 * rather than a sentence this component happens to own.
 *
 * The distinction matters and it is the test-quality finding this file also answers. `expect(screen
 * .getByText('Request refused'))` passes for as long as nobody edits that string, and keeps passing
 * after the state it was supposed to be about has changed underneath it — which is exactly how
 * "Unsafe part blocked" survived over plans where nothing was blocked. So the assertions below are
 * of the shape "given this server data, this word must be absent" and "given that server data, this
 * word must be present", driven by fixtures in the API's own shape.
 */

async function renderApp(variant = 'happy') {
  window.history.replaceState({}, '', `/?variant=${variant}`);
  await act(async () => { render(<Home />); });
}
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function click(name: string) { await act(async () => { fireEvent.click(screen.getAllByRole('button', { name })[0]!); }); }
async function clickSelector(selector: string) {
  const node = document.querySelector(selector);
  if (!node) throw new Error(`No element matched ${selector}`);
  await act(async () => { fireEvent.click(node); });
}
/** Open the newest row in the feed — the change itself, which is how the owner reaches it. */
async function openChange(id = SAMPLE_CHANGE_ID) { await clickSelector(`[data-change="${id}"] .feed-hit`); }
/** Reach a proposal the way the owner does, so the screen under test is the one they see. */
async function prepare() { await openChange(); await click('Handle this change'); await advance(2600); }
const decision = (over: Partial<Decision> & Pick<Decision, 'seq' | 'verdict'>): Decision => ({
  at: '2026-09-19T10:00:00+01:00', stage: 'validate', source: 'owner', untrusted: false,
  subject: 'a check', rule: 'a clause', detail: null, ...over,
});

beforeEach(() => { vi.useFakeTimers(); vi.stubGlobal('scrollTo', vi.fn()); resetSample(); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

/* ------------------------------------------------------------------ finding 5: refusal vs discarded --- */

test('a discarded slot does not put a refusal over the plan', async () => {
  // The fourth review's High, at the surface it was reported on. A sender offers 15:00 and 16:00; the
  // plan takes one and leaves the other. Nothing was stopped, so the summary must not say a security
  // check fired. The two cases differ in ONE verdict, so the difference in what is rendered can only
  // come from that verdict.
  const cases = [
    { name: 'a discarded slot and a withheld window', refusal: false, rows: [
      decision({ seq: 0, verdict: 'allowed', subject: 'Accepted 15:00–15:45' }),
      decision({ seq: 1, verdict: 'discarded', subject: 'Not chosen 16:00–16:45' }),
      decision({ seq: 2, verdict: 'withheld', source: 'system', stage: 'model', subject: 'A shared window is unavailable' }),
    ] },
    { name: 'a real refusal', refusal: true, rows: [
      decision({ seq: 0, verdict: 'refused', source: 'email', untrusted: true, subject: 'Share a private detail with an extra recipient' }),
    ] },
  ];
  for (const scenario of cases) {
    const spy = vi.spyOn(api, 'decisions').mockResolvedValue(scenario.rows);
    try {
      resetSample();
      await renderApp();
      await openChange();
      expect(screen.queryByText(/Unsafe part blocked/) !== null, scenario.name).toBe(scenario.refusal);
      fireEvent.click(screen.getByText('How this was checked'));
      expect(screen.queryByRole('region', { name: 'Blocked attempt' }) !== null, scenario.name).toBe(scenario.refusal);
    }
    finally { spy.mockRestore(); cleanup(); }
  }
});

/* ------------------------------------------------------------------ finding 6: per-effect partial --- */

test('a partial change with an unconfirmed delivery is never shown as a reply that did not send', async () => {
  // The sample's own `unknown` variant is exactly the case: the invite moved, the reply's delivery
  // was never confirmed. The change is `partial`, which used to be enough to print "The reply did not
  // send, so nothing else was told." — a statement about the world that nothing observed.
  await renderApp('unknown');
  await prepare();
  await click('Approve this change');
  await advance(2400);

  const outcome = await api.outcome(SAMPLE_CHANGE_ID);
  expect(outcome.effects.find(effect => effect.kind === 'email_reply')?.state).toBe('unknown');
  expect(outcome.effects.find(effect => effect.kind === 'calendar_update')?.state).toBe('done');

  const receipt = document.querySelector('.receipt-line')!;
  expect(receipt).toHaveTextContent(/Whether Northgate Clinic was told is unknown/);
  expect(receipt).not.toHaveTextContent(/did not send|was not told/);
  // And the calendar half, which IS confirmed, is still allowed to say so.
  expect(receipt).toHaveTextContent(/Moved Blood test — Rosa/);
  // The itemized list underneath reads from the same states, so the two cannot disagree.
  expect(screen.getByText('Email reply · unknown')).toBeInTheDocument();
  expect(screen.getByText('Calendar updated')).toBeInTheDocument();
});

test('a partial change whose reply genuinely failed says so, and says nothing about the other case', async () => {
  await renderApp('partial');
  await prepare();
  await click('Approve this change');
  await advance(2400);
  const outcome = await api.outcome(SAMPLE_CHANGE_ID);
  expect(outcome.effects.find(effect => effect.kind === 'email_reply')?.state).toBe('failed');
  const receipt = document.querySelector('.receipt-line')!;
  expect(receipt).toHaveTextContent(/Northgate Clinic was not told — the reply did not send/);
  expect(receipt).not.toHaveTextContent(/unknown/);
});

/* ------------------------------------------------------------------ finding 7: withheld is not refused --- */

test('a withheld decision never renders as a refusal anywhere on the change', async () => {
  const withheld = vi.spyOn(api, 'decisions').mockResolvedValue([
    decision({ seq: 0, verdict: 'allowed' }),
    decision({ seq: 1, verdict: 'withheld', source: 'system', stage: 'model', subject: 'A shared window is unavailable' }),
  ]);
  try {
    await renderApp();
    await openChange();
    expect(screen.queryByText(/Unsafe part blocked/)).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Blocked attempt' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('How this was checked'));
    expect(screen.queryByRole('region', { name: 'Blocked attempt' })).not.toBeInTheDocument();
    expect(screen.getByText(/planned around without being described/)).toBeInTheDocument();
  }
  finally { withheld.mockRestore(); }
});

/* ------------------------------------------------------------------ finding 8: autonomy is never a default --- */

test('an unread autonomy setting says unknown, never a default that sounds careful', async () => {
  const limits = vi.spyOn(api, 'limits').mockRejectedValue(new Error('403 from the owner gate'));
  try {
    await renderApp();
    await advance(50);
    const answer = document.querySelector('.standing-answer')!;
    expect(answer).toHaveTextContent(/could not be read/);
    // The value it must NOT show: the most permissive-sounding default in the set, presented as an
    // answer. Asserted as an absence so a future default cannot quietly reappear here.
    expect(answer).not.toHaveTextContent('Ask before acting');
    expect(answer).not.toHaveTextContent('Act within limits');
  }
  finally { limits.mockRestore(); }
});

test('a read autonomy setting still shows the value it read', async () => {
  await renderApp();
  await advance(50);
  expect(document.querySelector('.standing-answer')).toHaveTextContent('Ask before acting');
});

/* ------------------------------------------------------------------ finding 9: no affirmative headings over no data --- */

test('headings do not claim a check happened when the decision log did not load', async () => {
  const decisions = vi.spyOn(api, 'decisions').mockRejectedValue(new Error('offline'));
  try {
    await renderApp();
    await openChange();
    expect(screen.getByText(/the decision log did not load, so nothing below claims a check passed/)).toBeInTheDocument();
    fireEvent.click(screen.getByText('How this was checked'));
    // Column 3's title is the loud one: "What trusted code checked" over a log that never arrived.
    expect(screen.queryByRole('heading', { name: /What trusted code checked/ })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /The decision log did not load/ })).toBeInTheDocument();
    expect(screen.getByText(/No checks are assumed to have passed/)).toBeInTheDocument();
  }
  finally { decisions.mockRestore(); }
});

test('the model receipt disclosure does not name a receipt that was not returned', async () => {
  await renderApp();
  await openChange();
  // `modelView` is optional on `Api` (a backend that does not implement it), so it is replaced
  // rather than spied — the screen has to handle both the missing method and the refused read.
  const original = api.modelView;
  api.modelView = vi.fn().mockRejectedValue(new Error('nope'));
  try {
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '← Today' })); });
    await openChange();
    expect(screen.getByText('No model receipt was returned')).toBeInTheDocument();
    expect(screen.queryByText('What the model was shown')).not.toBeInTheDocument();
  }
  finally { api.modelView = original; }
});

/* ------------------------------------------------------------------ findings 11 & 12: no unsafe hrefs --- */

test('only an absolute http(s) URL ever reaches an href', async () => {
  // The helper, both directions, because a sanitiser that accepts nothing is as wrong as one that
  // accepts everything and only one of those shows up as a broken link.
  expect(safeHttpUrl('https://example.com/a')).toBe('https://example.com/a');
  expect(safeHttpUrl('http://127.0.0.1:8831/record/1')).toBe('http://127.0.0.1:8831/record/1');
  for (const bad of ['javascript:alert(1)', 'JavaScript:alert(1)', 'data:text/html,<script>', 'file:///etc/passwd', 'vbscript:x', '//evil.example/x', '/relative', 'not a url', '', null, undefined])
    expect(safeHttpUrl(bad as string)).toBeNull();

  // And through a rendered screen: a provider record whose URL is executable renders no link at all,
  // while the effect's own detail — the evidence — is still on the page.
  const preview = samplePreview();
  const poisoned: Change = {
    ...preview.change,
    stage: 'done',
    proposal: preview.change.proposal,
  };
  const outcome: Outcome = { summary: 'done', effects: [
    { kind: 'calendar_update', state: 'done', detail: 'A synthetic calendar result.', provider_url: 'javascript:alert(document.cookie)' },
    { kind: 'email_reply', state: 'done', detail: 'A synthetic reply result.', provider_url: 'https://mail.example/record/9' },
  ] };
  const { container } = render(<ChangeDetail
    today={{ ...preview.today, changes: [] } as Today}
    change={poisoned}
    summary={undefined}
    decisions={{ records: [], unavailable: false }}
    clauses={[]}
    outcome={outcome}
    modelView={null}
    modelViewState="unavailable"
    stale={false} expired={false} validBundle={false}
    owner={false} busy={false} offline={false} sample={false} editing={false} waited={false} preparing={false} executing={false}
    onBack={() => {}} onPrepare={() => {}} onApprove={() => {}} onLeave={() => {}} onStartEdit={() => {}} onWhy={() => {}}
  />);
  const hrefs = [...container.querySelectorAll('a[href]')].map(a => a.getAttribute('href')!);
  expect(hrefs.every(href => href.startsWith('http://') || href.startsWith('https://'))).toBe(true);
  expect(hrefs).not.toContain('javascript:alert(document.cookie)');
  expect(hrefs).toContain('https://mail.example/record/9');
  expect(screen.getByText('A synthetic calendar result.')).toBeInTheDocument();
});

/* ------------------------------------------------------------------ finding 10: completion checks the routine --- */

test('setup completion does not promise a routine that is not running', async () => {
  // The completion sentence is a claim about the background routine, so it is driven here by the
  // SESSION's own routine state rather than by anything the setup screen decided. `missing` is the
  // sample variant whose routine is off; the two renders below differ only in that field, so the
  // sentence is proved to follow it and not to be a constant.
  const live: Session['connections'] = [
    { service: 'gmail', state: 'connected', detail: '' },
    { service: 'calendar', state: 'connected', detail: '' },
  ];
  const base: Session = {
    mode: 'sample', authenticated_owner: true, owner_name: 'Sam', timezone: 'Europe/London',
    connections: live, routine: { enabled: false, paused: false, last_checked_at: null },
    boundary: { start: '16:30', buffer_minutes: 45, configured: true },
  };
  const props = { mode: 'ask_always' as const, chosen: true, taught: true, sample: true, onConnect: () => {}, onLimits: () => {}, onTeach: () => {}, onDone: () => {} };

  render(<FirstRun session={base} {...props} />);
  expect(screen.getByText(/Setup complete/)).toBeInTheDocument();
  expect(screen.queryByText(/routine keeps reading/)).not.toBeInTheDocument();
  expect(screen.getByText(/not switched on/)).toBeInTheDocument();
  cleanup();

  // The same screen with the routine running is allowed to say what it does — so the assertion above
  // is a distinction and not a phrase this component never emits.
  render(<FirstRun session={{ ...base, routine: { enabled: true, paused: false, last_checked_at: null } }} {...props} />);
  expect(screen.getByText(/routine keeps reading/)).toBeInTheDocument();
  cleanup();

  // Paused is not running, either: a paused routine reads nothing.
  render(<FirstRun session={{ ...base, routine: { enabled: true, paused: true, last_checked_at: null } }} {...props} />);
  expect(screen.queryByText(/routine keeps reading/)).not.toBeInTheDocument();
});

/* ------------------------------------------------------------------ finding 4: the token is a runtime value --- */

test('the live settings screen offers the owner token as a runtime entry, not a build variable', async () => {
  // The sample build does not show the field: there is no live transport to credential. The test
  // asserts the mechanism exists in the live client and that nothing in the environment can supply
  // it — the field is the mechanism, not a convenience over one.
  const { ownerToken, setOwnerToken, transportRefusal } = await import('@/lib/api');
  expect(ownerToken()).toBeNull();
  vi.stubEnv('NEXT_PUBLIC_OWNER_TOKEN', 'a-token-in-the-environment');
  expect(ownerToken()).toBeNull();
  setOwnerToken('entered-at-runtime');
  expect(ownerToken()).toBe('entered-at-runtime');
  setOwnerToken(null);
  expect(ownerToken()).toBeNull();
  // And the credential is refused a remote destination whatever the build was. The page origin is
  // passed explicitly rather than left to `window`: which PAGE this build was served from is the
  // question an empty base turns into, and a test that let jsdom answer it would be asserting about
  // the test runner's own URL.
  expect(transportRefusal('https://staging.example', 'http://localhost:3000')).toMatch(/not this machine/);
  expect(transportRefusal('http://127.0.0.1:8831', 'http://localhost:3000')).toBeNull();
  expect(transportRefusal('', 'http://localhost:3000')).toBeNull();
  expect(transportRefusal('', 'https://app.example')).toMatch(/not this machine/);
});
